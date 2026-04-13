"""Хэндлер скачивания видео и Reels из Facebook
Флоу: ссылка → сразу скачиваем → отправка
"""
import asyncio
import logging
import os
import time

from aiogram import F, Router
from aiogram.types import FSInputFile, Message

from bot.config import settings
from bot.database import async_session
from bot.database.crud import (
    get_cached_media,
    get_or_create_user,
    get_user_language,
    save_media_cache,
)
from bot.emojis import E
from bot.i18n import t
from bot.services.facebook import (
    DownloadResult,
    FileTooLargeError,
    classify_error,
    downloader,
)
from bot.utils.helpers import clean_facebook_url, is_facebook_url

logger = logging.getLogger(__name__)
router = Router()

# минимальный интервал обновления прогресса (Telegram лимит ~30 ред/мин)
PROGRESS_UPDATE_INTERVAL = 4

# троттлинг алертов о fallback (одно сообщение раз в N секунд)
_FALLBACK_ALERT_THROTTLE = 600  # 10 минут
_last_fallback_alert: dict[str, float] = {}

# человеко-понятные подписи к категориям ошибок
_ERROR_CATEGORY_LABELS = {
    "ip_blocked": "Facebook заблокировал IP — нужна ротация прокси",
    "login_required": "Требуется логин — обнови cookies через /update_cookies",
    "network": "Сетевая ошибка (таймаут/нет связи)",
    "unknown": "Неизвестная ошибка",
}

# категории которые не алертим — ошибки на стороне юзера/контента
_SILENT_CATEGORIES = {"unavailable"}

# максимум 20 одновременных скачиваний
_download_semaphore = asyncio.Semaphore(20)


def _make_progress_bar(percent: int, dl_mb: float, total_mb: float, lang: str = "ru") -> str:
    """Рисует полоску прогресса"""
    filled = int(percent / 100 * 12)
    bar = "▰" * filled + "▱" * (12 - filled)
    progress_label = t("download.progress", lang)
    size_label = t("download.progress_mb", lang, dl_mb=dl_mb, total_mb=total_mb)
    return (
        f"{E['clock']} {progress_label}\n"
        f"{bar} {percent}%\n"
        f"{size_label}"
    )


def _format_duration(seconds: int) -> str:
    """Форматирует секунды в MM:SS или HH:MM:SS"""
    if not seconds:
        return "—"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _get_error_text(error: str, lang: str = "ru") -> str:
    """Человеко-понятное сообщение об ошибке"""
    error_lower = error.lower()

    if "private" in error_lower or "login" in error_lower or "checkpoint" in error_lower:
        return t("error.private", lang)
    elif "not found" in error_lower or "404" in error_lower or "removed" in error_lower:
        return t("error.not_found", lang)
    elif "unavailable" in error_lower or "deleted" in error_lower:
        return t("error.unavailable", lang)
    elif "too large" in error_lower or "too big" in error_lower:
        return t("error.too_large", lang)
    elif "timeout" in error_lower:
        return t("error.timeout", lang)
    else:
        return t("error.generic", lang)


@router.message(F.text)
async def handle_facebook_url(message: Message) -> None:
    """Обработка текстовых сообщений — ищем ссылки Facebook и сразу скачиваем"""
    text = message.text.strip()

    async with async_session() as session:
        lang = await get_user_language(session, message.from_user.id)

    if not is_facebook_url(text):
        await message.answer(
            t("download.not_facebook", lang),
            parse_mode="HTML",
        )
        return

    clean_url = clean_facebook_url(text)
    await _process_download(message, clean_url, message.from_user, lang)


async def _process_download(
    message: Message,
    url: str,
    user,
    lang: str = "ru",
) -> None:
    """Скачивает и отправляет видео"""
    format_key = "video"

    # проверяем кэш
    async with async_session() as session:
        await get_or_create_user(
            session=session,
            telegram_id=user.id,
            username=user.username,
            full_name=user.full_name,
        )
        cached = await get_cached_media(session, url, format_key)

    if cached:
        logger.info("Кэш найден для %s", url)
        await _send_cached(message, cached.file_id, lang)
        return

    async with _download_semaphore:
        status_msg = await message.answer(t("download.processing", lang))

        # callback для обновления прогресса
        last_progress_update = {"time": 0}
        loop = asyncio.get_running_loop()

        def on_progress(dl_mb: float, total_mb: float, percent: int):
            """yt-dlp вызывает из другого потока, шедулим в asyncio"""
            now = time.time()
            if now - last_progress_update["time"] < PROGRESS_UPDATE_INTERVAL:
                return
            last_progress_update["time"] = now

            text = _make_progress_bar(percent, dl_mb, total_mb, lang)
            try:
                asyncio.run_coroutine_threadsafe(
                    _safe_edit(status_msg, text), loop
                )
            except Exception:
                pass

        result: DownloadResult | None = None
        try:
            result = await downloader.download_video(url, on_progress)
            file_id = await _send_media(message, result, status_msg, lang)

            # сохраняем в кэш
            if file_id:
                async with async_session() as session:
                    await save_media_cache(
                        session=session,
                        media_url=url,
                        format_key=format_key,
                        file_id=file_id,
                        media_type="video",
                    )
                    user_obj = await get_or_create_user(
                        session=session,
                        telegram_id=user.id,
                        username=user.username,
                        full_name=user.full_name,
                    )
                    user_obj.download_count += 1
                    await session.commit()

            # удаляем статусное сообщение
            try:
                await status_msg.delete()
            except Exception:
                pass

        except FileTooLargeError:
            try:
                await status_msg.edit_text(
                    t("error.too_large", lang),
                    parse_mode="HTML",
                )
            except Exception:
                await message.answer(
                    t("error.too_large", lang),
                    parse_mode="HTML",
                )

        except Exception as e:
            logger.error("Ошибка скачивания %s: %s", url, e)
            error_text = _get_error_text(str(e), lang)
            try:
                await status_msg.edit_text(error_text, parse_mode="HTML")
            except Exception:
                await message.answer(error_text, parse_mode="HTML")

        finally:
            if result:
                downloader.cleanup(result)


async def _send_media(
    message: Message, result: DownloadResult,
    status_msg=None, lang: str = "ru",
) -> str | None:
    """Отправляет видео юзеру и возвращает file_id"""
    file = FSInputFile(result.file_path)

    if status_msg:
        try:
            await status_msg.edit_text(t("download.uploading", lang))
        except Exception:
            pass

    t_upload = time.monotonic()
    try:
        size_mb = os.path.getsize(result.file_path) / 1024 / 1024
    except OSError:
        size_mb = 0

    promo = t("download.promo", lang, bot_username=settings.bot_username)
    sent = await message.answer_video(
        video=file,
        caption=f"{E['video']} {result.title}{promo}",
        duration=int(result.duration) if result.duration else None,
        width=result.width,
        height=result.height,
    )

    _log_upload_metric(t_upload, size_mb)
    return sent.video.file_id if sent.video else None


def _log_upload_metric(t_start: float, size_mb: float) -> None:
    elapsed = time.monotonic() - t_start
    speed = size_mb / elapsed if elapsed > 0 else 0
    logger.info(
        "[METRIC] upload_video %.2fs size=%.1fMB speed=%.1fMB/s",
        elapsed, size_mb, speed,
    )


async def _send_cached(message: Message, file_id: str, lang: str = "ru") -> None:
    """Отправляет из кэша по file_id"""
    try:
        promo = t("download.promo", lang, bot_username=settings.bot_username)
        await message.answer_video(
            video=file_id,
            caption=f"{E['video']} Facebook Video{promo}",
        )
    except Exception as e:
        logger.error("Ошибка отправки из кэша: %s", e)
        await message.answer(f"{E['warning']} Кэш устарел. Отправь ссылку ещё раз.")


async def _safe_edit(msg: Message, text: str) -> None:
    """Безопасно обновляет сообщение (игнорирует ошибки Telegram)"""
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# --- Алерты администратору о падении источников ---

_bot_ref = None


def setup_fallback_alerts(bot) -> None:
    """Подключает callback алертов админу к downloader.
    Вызывается из main.py после создания бота.
    """
    global _bot_ref
    _bot_ref = bot
    downloader.on_source_failed = _on_source_failed
    logger.info("Алерты о падении источников подключены")


def _on_source_failed(source: str, error: str) -> None:
    """Sync callback — шедулит асинхронную отправку алерта"""
    if _bot_ref is None:
        return
    try:
        asyncio.create_task(_send_fallback_alert(source, error))
    except RuntimeError:
        pass


async def _send_fallback_alert(source: str, error: str) -> None:
    """Отправляет алерт админу о падении источника. С троттлингом."""
    now = time.time()
    category = classify_error(error)

    if category in _SILENT_CATEGORIES:
        return

    throttle_key = f"{source}:{category}"
    last = _last_fallback_alert.get(throttle_key, 0)
    if now - last < _FALLBACK_ALERT_THROTTLE:
        return
    _last_fallback_alert[throttle_key] = now

    short_error = error[:300] + "..." if len(error) > 300 else error
    category_label = _ERROR_CATEGORY_LABELS.get(category, category)

    text = (
        f"{E['warning']} <b>Источник упал!</b>\n\n"
        f"<b>Источник:</b> {source}\n"
        f"<b>Категория:</b> {category_label}\n"
        f"<b>Ошибка:</b> <code>{short_error}</code>"
    )

    for admin_id in settings.admin_id_list:
        try:
            await _bot_ref.send_message(admin_id, text, parse_mode="HTML")
            logger.info("Админ %s уведомлён о падении %s (%s)", admin_id, source, category)
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)

"""Сервис скачивания видео и Reels из Facebook — yt-dlp
Fallback chain: direct → WARP → proxy → cookies-варианты
"""
import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from bot.config import settings

logger = logging.getLogger(__name__)

# лимит файла (Local Bot API — 2 ГБ)
MAX_FILE_SIZE = settings.max_file_size

# WARP SOCKS5 прокси (контейнер warp в docker-compose)
WARP_PROXY = "socks5://warp:9091"


@dataclass
class VideoInfo:
    """Информация о видео (до скачивания)"""
    title: str
    duration: int  # в секундах
    thumbnail: str | None = None
    uploader: str | None = None
    is_live: bool = False


@dataclass
class DownloadResult:
    """Результат скачивания"""
    file_path: str
    title: str
    duration: int | None = None
    thumbnail: str | None = None
    width: int | None = None
    height: int | None = None


# тип для callback прогресса: (скачано_мб, всего_мб, процент)
ProgressCallback = Callable[[float, float, int], None] | None


class FileTooLargeError(Exception):
    """Файл превышает лимит Telegram (2 ГБ)"""
    pass


def classify_error(error_msg: str) -> str:
    """Классифицирует ошибку yt-dlp в категорию для алертов.
    Возвращает: 'ip_blocked', 'login_required', 'network', 'unavailable', 'unknown'.
    """
    msg = error_msg.lower()

    if "403" in msg or "forbidden" in msg or "rate limit" in msg:
        return "ip_blocked"

    if "login" in msg or "requires login" in msg or "private" in msg or "checkpoint" in msg:
        return "login_required"

    if "timeout" in msg or "connection" in msg or "socks" in msg:
        return "network"

    if "unavailable" in msg or "not found" in msg or "removed" in msg or "deleted" in msg:
        return "unavailable"

    return "unknown"


class FacebookDownloader:
    """Скачивает Facebook видео/Reels через yt-dlp.
    Fallback chain: direct → WARP → proxy → cookies-варианты.
    """

    _COOKIES_PATH = "/app/cookies/cookies.txt"

    def __init__(self):
        # базовая директория для скачиваний (per-download создаётся в download_video)
        self.base_download_dir = tempfile.mkdtemp(prefix="fb_bot_")
        # callback для уведомления админа о падении источника
        self.on_source_failed: Callable[[str, str], None] | None = None

    def has_cookies(self) -> bool:
        return os.path.isfile(self._COOKIES_PATH)

    def _fire_source_failed(self, source: str, error: Exception) -> None:
        """Триггер callback'а о падении источника"""
        if self.on_source_failed is None:
            return
        try:
            self.on_source_failed(source, str(error))
        except Exception as e:
            logger.warning("on_source_failed callback упал: %s", e)

    def _cleanup_old_files(self, max_age_minutes: int = 30) -> None:
        import shutil
        now = time.time()
        cutoff = now - max_age_minutes * 60
        try:
            for entry in os.listdir(self.base_download_dir):
                entry_path = os.path.join(self.base_download_dir, entry)
                if os.path.isdir(entry_path) and os.path.getmtime(entry_path) < cutoff:
                    shutil.rmtree(entry_path, ignore_errors=True)
                    logger.info("Очистка старой директории: %s", entry)
                elif os.path.isfile(entry_path) and os.path.getmtime(entry_path) < cutoff:
                    os.remove(entry_path)
                    logger.info("Очистка старого файла: %s", entry)
        except OSError as e:
            logger.warning("Ошибка при очистке: %s", e)

    # --- Опции для разных источников ---

    def _direct_opts(self) -> dict:
        """Прямое подключение (без прокси)"""
        return {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 3,
        }

    def _warp_opts(self) -> dict:
        """WARP — через Cloudflare WARP SOCKS5"""
        return {
            "quiet": True,
            "no_warnings": True,
            "proxy": WARP_PROXY,
            "socket_timeout": 30,
            "retries": 3,
        }

    def _proxy_opts(self) -> dict | None:
        """Резидентный прокси (если задан в .env). Возвращает None если не задан."""
        proxy = getattr(settings, "proxy_url", "") or ""
        if not proxy:
            return None
        return {
            "quiet": True,
            "no_warnings": True,
            "proxy": proxy,
            "socket_timeout": 30,
            "retries": 3,
        }

    def _warp_cookies_opts(self) -> dict:
        """WARP + cookies"""
        return {
            **self._warp_opts(),
            "cookiefile": self._COOKIES_PATH,
        }

    def _proxy_cookies_opts(self) -> dict:
        """Прокси + cookies. Если proxy не задан — fallback на WARP + cookies."""
        base = self._proxy_opts()
        if base is None:
            base = self._warp_opts()
        return {
            **base,
            "cookiefile": self._COOKIES_PATH,
        }

    # --- Публичные методы ---

    async def get_info(self, url: str) -> VideoInfo:
        """Получает метаданные видео. Fallback: direct → WARP → proxy."""
        import yt_dlp

        t_start = time.monotonic()
        loop = asyncio.get_running_loop()

        # собираем источники: direct (primary) → WARP → proxy
        sources = [
            ("direct", self._direct_opts()),
            ("warp", self._warp_opts()),
        ]
        proxy_opts = self._proxy_opts()
        if proxy_opts is not None:
            sources.append(("proxy", proxy_opts))

        last_error = None
        source = "direct"
        info = None

        for source_name, opts in sources:
            ydl_opts = {
                **opts,
                "skip_download": True,
                "ignore_no_formats_error": True,
            }
            try:
                info = await loop.run_in_executor(
                    None, self._extract_info, url, ydl_opts
                )
                source = source_name
                break
            except Exception as e:
                last_error = e
                cat = classify_error(str(e))
                if cat in ("unavailable", "login_required"):
                    raise
                self._fire_source_failed(source_name, e)
                logger.warning("%s не дал инфо: %s", source_name, e)

        if info is None:
            raise last_error or RuntimeError("get_info_failed")

        elapsed = time.monotonic() - t_start
        logger.info("[METRIC] get_info %.2fs source=%s url=%s", elapsed, source, url)

        return VideoInfo(
            title=info.get("title", "Facebook Video"),
            duration=int(info.get("duration") or 0),
            thumbnail=info.get("thumbnail"),
            uploader=info.get("uploader"),
            is_live=bool(info.get("is_live")),
        )

    async def download_video(
        self, url: str,
        progress_callback: ProgressCallback = None,
    ) -> DownloadResult:
        """Скачивает видео (best mp4).
        Fallback chain: WARP → proxy → WARP+cookies → proxy+cookies.
        Per-download подпапка для изоляции от других юзеров.
        """
        # cleanup старых файлов — через executor чтобы не блокировать event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._cleanup_old_files)

        t_start = time.monotonic()

        # создаём изолированную подпапку для этого скачивания
        download_dir = tempfile.mkdtemp(prefix="dl_", dir=self.base_download_dir)

        # direct (primary) → WARP → proxy → direct+cookies → WARP+cookies → proxy+cookies
        sources = [
            ("direct", self._direct_opts()),
            ("warp", self._warp_opts()),
        ]
        proxy_opts = self._proxy_opts()
        if proxy_opts is not None:
            sources.append(("proxy", proxy_opts))
        if self.has_cookies():
            sources.append(("direct+cookies", {**self._direct_opts(), "cookiefile": self._COOKIES_PATH}))
            sources.append(("warp+cookies", self._warp_cookies_opts()))
            if proxy_opts is not None:
                sources.append(("proxy+cookies", self._proxy_cookies_opts()))

        last_error = None
        try:
            for source_name, opts in sources:
                try:
                    result = await self._do_download(url, progress_callback, opts, download_dir)
                    checked = self._check_size(result)
                    self._log_metric("download_video", t_start, source_name, checked.file_path)
                    return checked
                except FileTooLargeError:
                    raise
                except Exception as e:
                    last_error = e
                    logger.warning("%s не сработал: %s", source_name, e)
                    # ошибка на стороне контента — дальше не пробуем
                    cat = classify_error(str(e))
                    if cat in ("unavailable", "login_required"):
                        self._fire_source_failed(source_name, e)
                        raise
                    self._fire_source_failed(source_name, e)

            raise last_error or RuntimeError("download_failed")
        except Exception:
            # при ошибке чистим подпапку
            import shutil
            shutil.rmtree(download_dir, ignore_errors=True)
            raise

    # --- Внутренние методы ---

    def _extract_info(self, url: str, opts: dict) -> dict:
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    async def _do_download(
        self, url: str,
        progress_callback: ProgressCallback,
        opts: dict,
        download_dir: str | None = None,
    ) -> DownloadResult:
        """Скачивает видео в mp4"""
        import yt_dlp

        dl_dir = download_dir or self.base_download_dir
        output_template = os.path.join(dl_dir, "%(id)s.%(ext)s")
        format_str = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"

        ydl_opts = {
            **opts,
            "format": format_str,
            "outtmpl": output_template,
            "merge_output_format": "mp4",
        }

        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None, self._download, url, ydl_opts, progress_callback
        )

        file_path = self._find_downloaded_file(info, "mp4", dl_dir)
        if not file_path or not os.path.exists(file_path):
            raise RuntimeError("Не удалось найти скачанный видеофайл")

        return DownloadResult(
            file_path=file_path,
            title=info.get("title", "Facebook Video"),
            duration=int(info.get("duration") or 0),
            thumbnail=info.get("thumbnail"),
            width=info.get("width"),
            height=info.get("height"),
        )

    def _download(self, url: str, opts: dict, progress_callback: ProgressCallback = None) -> dict:
        import yt_dlp

        if progress_callback:
            last_update = {"time": 0}

            def _hook(d):
                if d["status"] != "downloading":
                    return
                now = time.time()
                if now - last_update["time"] < 3:
                    return
                last_update["time"] = now

                downloaded = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                if total > 0:
                    percent = int(downloaded / total * 100)
                    dl_mb = downloaded / 1024 / 1024
                    total_mb = total / 1024 / 1024
                    progress_callback(dl_mb, total_mb, percent)

            opts["progress_hooks"] = [_hook]

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    def _find_downloaded_file(self, info: dict, expected_ext: str, download_dir: str | None = None) -> str | None:
        dl_dir = download_dir or self.base_download_dir
        video_id = info.get("id", "")
        # сначала ищем по id
        for filename in os.listdir(dl_dir):
            if video_id and video_id in filename and filename.endswith(f".{expected_ext}"):
                return os.path.join(dl_dir, filename)
        # fallback — последний файл с нужным расширением
        for filename in sorted(os.listdir(dl_dir), reverse=True):
            if filename.endswith(f".{expected_ext}"):
                return os.path.join(dl_dir, filename)
        return None

    def _check_size(self, result: DownloadResult) -> DownloadResult:
        file_size = os.path.getsize(result.file_path)
        if file_size > MAX_FILE_SIZE:
            self._remove_file(result.file_path)
            raise FileTooLargeError(
                f"Файл слишком большой ({file_size / 1024 / 1024:.0f} МБ)"
            )
        return result

    def _log_metric(self, op: str, t_start: float, source: str, file_path: str) -> None:
        elapsed = time.monotonic() - t_start
        try:
            size_mb = os.path.getsize(file_path) / 1024 / 1024
        except OSError:
            size_mb = 0
        speed = size_mb / elapsed if elapsed > 0 else 0
        logger.info(
            "[METRIC] %s %.2fs source=%s size=%.1fMB speed=%.1fMB/s",
            op, elapsed, source, size_mb, speed,
        )

    def cleanup(self, result: DownloadResult) -> None:
        self._remove_file(result.file_path)

    def _remove_file(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info("Удалён: %s", path)
        except OSError as e:
            logger.warning("Не удалось удалить файл: %s", e)


# глобальный экземпляр
downloader = FacebookDownloader()

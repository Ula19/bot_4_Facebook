"""Точка входа — запуск Facebook-бота"""
import asyncio
import logging
import os
import sys
import time

# uvloop ускоряет asyncio в 2-4 раза (не работает на Windows!)
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass  # на Windows — работаем без uvloop

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings

# настраиваем логирование
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# флаг-файл для crash recovery
CRASH_FLAG = ".crash_flag"


async def main() -> None:
    """Инициализация и запуск бота"""
    # подключаемся к Local Bot API если указан URL
    session = None
    api_url = settings.bot_api_url
    if api_url != "https://api.telegram.org":
        # Local Bot API — файлы до 2 ГБ
        # Увеличиваем таймаут для загрузки больших файлов (по умолчанию 60 сек)
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(api_url, is_local=True),
            timeout=600  # 10 минут на запрос
        )
        logger.info(f"Local Bot API: {api_url}")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())

    # подключаем хэндлеры (порядок важен!)
    from bot.handlers import start, admin, cookies, download
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(cookies.router)
    dp.include_router(download.router)  # последний — ловит Facebook-ссылки

    # подключаем алерты о падении источников
    from bot.handlers.download import setup_fallback_alerts
    setup_fallback_alerts(bot)

    # подключаем мидлвари
    from bot.middlewares.rate_limit import RateLimitMiddleware
    from bot.middlewares.subscription import SubscriptionMiddleware

    dp.message.middleware(RateLimitMiddleware())
    dp.message.middleware(SubscriptionMiddleware())
    dp.callback_query.middleware(SubscriptionMiddleware())

    # события старта и остановки
    # путь к данным Local Bot API (volume bot-api-data)
    LOCAL_BOT_API_DIR = "/var/lib/telegram-bot-api"
    # файлы Local Bot API старше этого возраста удаляются (часы)
    BOT_API_CLEANUP_MAX_AGE_HOURS = 1

    async def _background_cleanup() -> None:
        """Фоновая задача: очистка памяти, /tmp и файлов Local Bot API каждые 5 минут"""
        import glob
        import shutil
        from bot.middlewares.rate_limit import cleanup_stale_entries
        while True:
            await asyncio.sleep(300)  # 5 минут
            # чистим протухшие записи rate limit (memory leak)
            removed = cleanup_stale_entries()
            if removed:
                logger.info("Фоновая очистка: удалено %d записей rate limit", removed)
            # чистим старые файлы /tmp/fb_bot (старше 30 минут)
            now = time.time()
            cutoff = now - 30 * 60
            cleaned = 0
            for f in glob.glob("/tmp/fb_bot/**/*", recursive=True):
                try:
                    if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                        os.remove(f)
                        cleaned += 1
                except OSError:
                    pass
            if cleaned:
                logger.info("Фоновая очистка: удалено %d временных файлов из /tmp/fb_bot", cleaned)
            # чистим файлы Local Bot API (volume bot-api-data)
            await asyncio.get_running_loop().run_in_executor(
                None, _cleanup_bot_api_files, now
            )

    def _cleanup_bot_api_files(now: float) -> None:
        """Удаляет старые файлы из Local Bot API volume"""
        import shutil
        cutoff = now - BOT_API_CLEANUP_MAX_AGE_HOURS * 3600
        if not os.path.isdir(LOCAL_BOT_API_DIR):
            return
        cleaned_files = 0
        cleaned_bytes = 0
        try:
            for root, dirs, files in os.walk(LOCAL_BOT_API_DIR):
                for name in files:
                    filepath = os.path.join(root, name)
                    try:
                        stat = os.stat(filepath)
                        if stat.st_mtime < cutoff:
                            cleaned_bytes += stat.st_size
                            os.remove(filepath)
                            cleaned_files += 1
                    except OSError:
                        pass
                # удаляем пустые подпапки
                for name in dirs:
                    dirpath = os.path.join(root, name)
                    try:
                        if not os.listdir(dirpath):
                            os.rmdir(dirpath)
                    except OSError:
                        pass
        except OSError as e:
            logger.warning("Ошибка при очистке Local Bot API: %s", e)
        if cleaned_files:
            mb = cleaned_bytes / 1024 / 1024
            logger.info(
                "Очистка Local Bot API: удалено %d файлов (%.1f МБ)",
                cleaned_files, mb,
            )

    @dp.startup()
    async def on_startup() -> None:
        # создаём таблицы в БД
        from bot.database import engine
        from bot.database.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Таблицы БД созданы")

        # проверяем crash recovery
        if os.path.exists(CRASH_FLAG):
            logger.warning("Обнаружен crash-flag — предыдущий запуск завершился аварийно")
            os.remove(CRASH_FLAG)

        # ставим crash-flag (уберём при нормальном завершении)
        with open(CRASH_FLAG, "w") as f:
            f.write("running")

        # запускаем фоновую очистку
        asyncio.create_task(_background_cleanup())
        logger.info("Фоновая очистка запущена (интервал 5 мин)")

        bot_info = await bot.get_me()
        logger.info(f"Бот @{bot_info.username} запущен!")

        # ставим дефолтное меню команд (глобально, ru — для новых юзеров)
        from bot.utils.commands import set_default_commands
        await set_default_commands(bot)
        logger.info("Дефолтное меню команд установлено")

    @dp.shutdown()
    async def on_shutdown() -> None:
        # убираем crash-flag при нормальном завершении
        if os.path.exists(CRASH_FLAG):
            os.remove(CRASH_FLAG)
        logger.info("Бот остановлен")

    # запускаем polling
    try:
        logger.info("Запуск polling...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

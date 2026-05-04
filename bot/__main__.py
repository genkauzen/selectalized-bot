import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from . import brute_worker, db, notify
from .config import config
from .handlers import router
from .tg_format import SEP, bold

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _on_startup(bot: Bot) -> None:
    await db.init_db()
    notify.set_bot(bot)

    # If the worker was running before restart, resume automatically
    if await db.is_running():
        await brute_worker.start_worker()
        await notify.logs(
            f"♻️ {bold('Бот перезапущен')} — перебор возобновлён"
        )
    else:
        await notify.logs(
            f"🤖 {bold('Selectalized Bot запущен')}\n"
            f"{SEP}\n"
            f"Статус : ⏹ ожидание\n"
            f"Команды: /help"
        )


async def _on_shutdown(bot: Bot) -> None:
    await brute_worker.stop_worker()
    logger.info("Shutdown complete")


async def main() -> None:
    if not config.bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    # Build bot session (with optional SOCKS5 proxy)
    session_kwargs = {}
    if config.tg_proxy_use and config.tg_proxy_url:
        session_kwargs["proxy"] = config.tg_proxy_url

    session = AiohttpSession(**session_kwargs) if session_kwargs else None

    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(_on_startup)
    dp.shutdown.register(_on_shutdown)

    logger.info("Starting Selectalized Bot…")
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())

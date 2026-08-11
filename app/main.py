"""Точка входа Telegram-бота."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app import config
from app.caldav_service import CalDAVError, get_client
from app.handlers import router


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.LOG_LEVEL == "DEBUG":
        for noisy in ("openai", "httpcore", "httpx", "urllib3", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    if not config.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в .env")
    if not config.ALLOWED_USER_IDS:
        logging.warning("ALLOWED_USER_IDS не"
                        " задан — доступ запрещён всем")

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    async def preflight() -> None:
        try:
            await asyncio.to_thread(get_client)
            logging.info("CalDAV подключение установлено: %s", config.CALDAV_URL)
        except CalDAVError as exc:
            logging.error("CalDAV недоступен: %s", exc)

    await preflight()
    dp.include_router(router)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

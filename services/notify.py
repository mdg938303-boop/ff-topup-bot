import logging

from aiogram import Bot

from config import ADMIN_IDS

logger = logging.getLogger(__name__)


async def notify_admins(bot: Bot, text: str) -> None:
    """Best-effort message to every admin (an admin who blocked the bot must not break anything)."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not notify admin %s: %s", admin_id, exc)


async def notify_user(bot: Bot, user_id: int, text: str) -> bool:
    try:
        await bot.send_message(user_id, text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not notify user %s: %s", user_id, exc)
        return False

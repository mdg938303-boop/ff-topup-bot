from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from database.database import get_session
from database.models import User
from utils.helpers import is_admin

BANNED_TEXT = "🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে। সাহায্যের জন্য সাপোর্টে যোগাযোগ করুন।"


class BanMiddleware(BaseMiddleware):
    """
    Blocks banned users from EVERYTHING (before, only /start and ordering checked —
    a banned user could still deposit and use the wallet). Admins are never blocked.
    """

    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        if tg_user is not None and not is_admin(tg_user.id):
            async with get_session() as session:
                banned = (await session.execute(
                    select(User.is_banned).where(User.id == tg_user.id)
                )).scalar_one_or_none()
            if banned:
                if isinstance(event, CallbackQuery):
                    await event.answer(BANNED_TEXT, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(BANNED_TEXT)
                return None
        return await handler(event, data)

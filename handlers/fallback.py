"""
Last router. Catches photos/stickers/voice sent while a text-only form is waiting
for input (they used to crash the handlers with AttributeError on message.text).
Handlers that really accept any content (admin broadcast) live in earlier routers.
"""
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

router = Router(name="fallback")


@router.message(~StateFilter(None), ~F.text)
async def non_text_in_form(message: Message):
    await message.answer("⚠️ এখানে শুধু টেক্সট লিখে পাঠান।")

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import desc, func, select

from config import PAGE_SIZE
from database.database import get_session
from database.models import Transaction, User
from keyboards.user import txns_list_kb
from services.wallet import get_or_create_user
from utils.helpers import esc, fmt_money

router = Router(name="referral")

_NO_INLINE = InlineKeyboardMarkup(inline_keyboard=[])

_bot_username: str | None = None  # cached: get_me() is a network call, the username never changes


async def _get_bot_username(bot) -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return _bot_username


async def _render_referral_text(bot, session, tg_user) -> str:
    user = await get_or_create_user(session, tg_user)
    await session.commit()
    result = await session.execute(select(func.count(User.id)).where(User.referred_by == user.id))
    total_referred = result.scalar() or 0

    link = f"https://t.me/{await _get_bot_username(bot)}?start={user.referral_code}"

    return (
        "🎁 <b>Referral Program</b>\n\n"
        "আপনার friend-দের নিচের link দিয়ে invite করুন। তারা নতুন অ্যাকাউন্ট খুলে প্রথম উল্লেখযোগ্য deposit "
        "করলে আপনি bonus পাবেন!\n\n"
        f"🔗 Your link:\n<code>{esc(link)}</code>\n\n"
        f"👥 Total Referred: {total_referred}\n"
        f"💰 Total Earnings: {fmt_money(user.referral_earnings)}"
    )


@router.callback_query(F.data == "referral")
async def cb_referral(call: CallbackQuery):
    async with get_session() as session:
        text = await _render_referral_text(call.bot, session, call.from_user)
    await call.message.edit_text(text, reply_markup=_NO_INLINE)
    await call.answer()


async def _render_txns_page(session, user_id: int, page: int):
    result = await session.execute(
        select(Transaction).where(Transaction.user_id == user_id)
        .order_by(desc(Transaction.created_at)).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
    )
    txns = result.scalars().all()
    has_next = len(txns) > PAGE_SIZE
    txns = txns[:PAGE_SIZE]

    if not txns and page == 0:
        return "📜 কোনো transaction পাওয়া যায়নি।", None

    type_emoji = {"DEPOSIT": "➕", "PURCHASE": "🛒", "REFUND": "↩️", "REFERRAL_BONUS": "🎁", "ADJUSTMENT": "⚙️"}
    lines = ["📜 <b>Transaction History</b>\n"]
    for t in txns:
        sign = "+" if t.type.value in ("DEPOSIT", "REFUND", "REFERRAL_BONUS", "ADJUSTMENT") else "-"
        lines.append(
            f"{type_emoji.get(t.type.value, '•')} {t.type.value} {sign}{fmt_money(t.amount)} — "
            f"{t.created_at.strftime('%Y-%m-%d %H:%M')}"
        )
    return "\n".join(lines), txns_list_kb(page, has_next)


@router.callback_query(F.data.startswith("txns:"))
async def cb_transactions(call: CallbackQuery):
    page = int(call.data.split(":")[1])
    async with get_session() as session:
        text, kb = await _render_txns_page(session, call.from_user.id, page)
    await call.message.edit_text(text, reply_markup=kb or _NO_INLINE)
    await call.answer()

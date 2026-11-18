import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from sqlalchemy import desc, select

from config import PAGE_SIZE, SERVER_LABEL
from database.database import get_session
from database.models import Order, Package
from keyboards.user import back_home, orders_list_kb
from services.notify import notify_admins
from services.orders import (
    DuplicateOrderError, PackageUnavailableError, PriceChangedError, ProviderDispatchError,
    get_lock, place_order,
)
from services.wallet import InsufficientBalanceError, get_or_create_user
from utils.helpers import esc, fmt_money

logger = logging.getLogger(__name__)

router = Router(name="orders")

_NO_INLINE = InlineKeyboardMarkup(inline_keyboard=[])


@router.callback_query(F.data.startswith("order_place:"))
async def cb_order_place(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    pkg_id = int(parts[1])
    try:
        expected_price = Decimal(parts[2]) if len(parts) > 2 else None
    except InvalidOperation:
        expected_price = None

    # --- CONSUME the confirmation exactly once -------------------------------------
    # Telegram can deliver a double-tap as two concurrent updates. The check and the
    # state.clear() happen under one lock, so the second tap finds the state already
    # gone and stops here (before, both read the state, then both placed an order).
    async with get_lock("claim", call.from_user.id):
        data = await state.get_data()
        player_id = data.get("player_id")
        if not player_id or data.get("pkg_id") != pkg_id:
            await call.answer("এই অর্ডারটি আগেই প্রসেস হয়েছে বা মেয়াদ শেষ। 📦 My Orders চেক করুন।", show_alert=True)
            return
        await state.clear()

    await call.answer()  # stop the button spinner right away

    async with get_session() as session:
        user = await get_or_create_user(session, call.from_user)
        await session.commit()

        if user.is_banned:
            await call.message.edit_text("🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে।", reply_markup=_NO_INLINE)
            return

        try:
            result = await place_order(session, user, pkg_id, player_id, expected_price)

        except InsufficientBalanceError:
            pkg = (await session.execute(select(Package).where(Package.id == pkg_id))).scalar_one_or_none()
            price = pkg.price if pkg else expected_price
            await session.refresh(user)
            await call.message.edit_text(
                f"❌ <b>Insufficient Balance</b>\n\nআপনার ব্যালেন্স {fmt_money(user.balance)}, "
                f"কিন্তু প্যাকেজের মূল্য {fmt_money(price)}।\n\n"
                "নিচের ➕ Deposit বাটনে চেপে ব্যালেন্স যোগ করুন।",
                reply_markup=_NO_INLINE,
            )
            return

        except PriceChangedError as exc:
            await call.message.edit_text(
                f"⚠️ <b>প্যাকেজের দাম বদলে গেছে।</b>\n\nনতুন দাম: {fmt_money(exc.new_price)}\n"
                "কোনো টাকা কাটা হয়নি। 💎 Top-up করুন থেকে আবার অর্ডার করুন।",
                reply_markup=_NO_INLINE,
            )
            return

        except DuplicateOrderError:
            await call.message.edit_text(
                "⚠️ এই একই অর্ডার এইমাত্র দেওয়া হয়েছে। 📦 My Orders-এ চেক করুন — আবার চার্জ করা হয়নি।",
                reply_markup=_NO_INLINE,
            )
            return

        except PackageUnavailableError:
            await call.message.edit_text("❌ এই package টি আর available নেই।", reply_markup=_NO_INLINE)
            return

        except ProviderDispatchError as exc:
            money_line = (
                "💰 আপনার টাকা wallet-এ ফেরত দেওয়া হয়েছে।" if exc.refunded else "💰 কোনো টাকা কাটা হয়নি।"
            )
            await call.message.edit_text(
                f"❌ <b>এই মুহূর্তে top-up করা যাচ্ছে না।</b>\n\n{esc(str(exc))}\n\n"
                f"{money_line} একটু পরে আবার চেষ্টা করুন।",
                reply_markup=_NO_INLINE,
            )
            # The raw provider message (e.g. "insufficient funds") goes to admins only — never to customers.
            await notify_admins(call.bot, (
                "⚠️ <b>Provider rejected an order</b>\n\n"
                f"Order: {esc(exc.order_code) or '—'}\nUser: <code>{call.from_user.id}</code>\n"
                f"Reason: {esc(exc.detail)[:300]}"
            ))
            return

    order = result.order

    if result.uncertain:
        await call.message.edit_text(
            "⏳ <b>আপনার অর্ডার যাচাই করা হচ্ছে।</b>\n\n"
            f"🆔 Order ID: <code>{esc(order.order_code)}</code>\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n\n"
            "প্রোভাইডার থেকে নিশ্চিত উত্তর পাওয়া যায়নি। আপনার টাকা নিরাপদ আছে — "
            "Admin যাচাই করে top-up সম্পন্ন করবেন অথবা টাকা ফেরত দেবেন।",
            reply_markup=_NO_INLINE,
        )
        await notify_admins(call.bot, (
            "🚨 <b>Provider response UNCERTAIN — check manually</b>\n\n"
            f"🆔 {esc(order.order_code)}\n"
            f"👤 {esc(call.from_user.first_name)} (<code>{call.from_user.id}</code>)\n"
            f"💎 {esc(order.package_name_snapshot)}\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n"
            f"💰 {fmt_money(order.price_paid)}\n"
            f"Error: {esc(result.detail)[:300]}\n\n"
            "⚠️ প্রোভাইডারের প্যানেলে দেখুন অর্ডারটা আসলে গেছে কিনা, তারপর Admin Panel → Orders থেকে "
            "Mark Delivered বা Cancel & Refund করুন। (দ্বিতীয়বার পাঠাবেন না — double top-up হতে পারে।)"
        ))
        return

    is_auto = order.status.value == "PROCESSING"
    if is_auto:
        text = (
            "✅ <b>আপনার অর্ডার গ্রহণ করা হয়েছে!</b>\n\n"
            f"🆔 Order ID: <code>{esc(order.order_code)}</code>\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n"
            "⚡ Status: Processing (automatic)\n\n"
            "আপনার top-up প্রসেস হচ্ছে, কিছুক্ষণের মধ্যেই সম্পন্ন হয়ে যাবে এবং আপনাকে জানানো হবে।"
        )
    else:
        text = (
            "✅ <b>আপনার অর্ডার সফলভাবে গ্রহণ করা হয়েছে!</b>\n\n"
            f"🆔 Order ID: <code>{esc(order.order_code)}</code>\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n"
            "🟡 Status: Pending Delivery\n\n"
            "কিছুক্ষণের মধ্যে top-up হয়ে যাবে, আপনাকে নোটিফাই করা হবে।"
        )
    await call.message.edit_text(text, reply_markup=_NO_INLINE)

    if not is_auto:
        await notify_admins(call.bot, (
            "🔔 <b>New Top-up Order (Manual)</b>\n\n"
            f"🆔 {esc(order.order_code)}\n"
            f"👤 {esc(call.from_user.first_name)} (<code>{call.from_user.id}</code>)\n"
            f"💎 {esc(order.package_name_snapshot)}\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n"
            f"🌍 Server: {SERVER_LABEL}\n"
            f"💰 {fmt_money(order.price_paid)}"
        ))


async def _render_orders_page(session, user_id: int, page: int):
    result = await session.execute(
        select(Order).where(Order.user_id == user_id)
        .order_by(desc(Order.created_at)).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
    )
    orders = result.scalars().all()
    has_next = len(orders) > PAGE_SIZE
    orders = orders[:PAGE_SIZE]
    if not orders and page == 0:
        return "📦 আপনার কোনো অর্ডার নেই।", None
    return "📦 <b>My Orders</b>\n\nদেখতে অর্ডারে ট্যাপ করুন:", orders_list_kb(orders, page, has_next)


@router.callback_query(F.data.startswith("orders:"))
async def cb_my_orders(call: CallbackQuery, state: FSMContext):
    await state.clear()
    page = int(call.data.split(":")[1])
    async with get_session() as session:
        text, kb = await _render_orders_page(session, call.from_user.id, page)
    await call.message.edit_text(text, reply_markup=kb or _NO_INLINE)
    await call.answer()


@router.callback_query(F.data.startswith("order_view:"))
async def cb_order_view(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    async with get_session() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()

        if order is None or order.user_id != call.from_user.id:
            await call.answer("❌ Order not found.", show_alert=True)
            return

        status_map = {
            "PENDING_DELIVERY": "🟡 Pending Delivery",
            "PROCESSING": "⚡ Processing (automatic)",
            "DELIVERED": "🟢 Delivered",
            "CANCELLED": "🔴 Cancelled",
        }
        status_text = status_map.get(order.status.value, order.status.value)
        if order.needs_review and order.status.value == "PROCESSING":
            status_text = "⏳ যাচাই করা হচ্ছে (Admin দেখছেন)"
        text = (
            f"📦 <b>Order {esc(order.order_code)}</b>\n\n"
            f"💎 Package: {esc(order.package_name_snapshot)}\n"
            f"🎮 Player ID: <code>{esc(order.player_id)}</code>\n"
            f"💰 Price: {fmt_money(order.price_paid)}\n"
            f"📅 Date: {order.created_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"Status: {status_text}"
        )
        if order.delivery_note:
            text += f"\n\n📝 Note: {esc(order.delivery_note)}"

    await call.message.edit_text(text, reply_markup=back_home("orders:0"))
    await call.answer()

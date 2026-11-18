import logging
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import MAX_DEPOSIT, MIN_DEPOSIT, PUBLIC_BASE_URL
from database.database import get_session
from database.models import PaymentIntent, PaymentMethod
from keyboards.user import cancel_kb, deposit_choice_kb, deposit_methods_kb
from services.auto_deposit import deposit_success_text, verify_and_record
from services.mraipay_client import MrAiPayClient, MrAiPayError
from services.notify import notify_admins
from services.orders import get_lock
from services.payments import (
    DuplicateTransactionError, TooManyPendingError, create_deposit_request, get_mraipay_config, is_duplicate_txn,
)
from services.wallet import get_or_create_user
from utils.helpers import esc, fmt_money, normalize_txn_id, parse_amount, parse_phone
from utils.states import AutoDepositFlow, DepositFlow

logger = logging.getLogger(__name__)

router = Router(name="deposit")

_NO_INLINE = InlineKeyboardMarkup(inline_keyboard=[])

_AMOUNT_HINT = f"⚠️ সঠিক পরিমাণ লিখুন ({fmt_money(MIN_DEPOSIT)} – {fmt_money(MAX_DEPOSIT)}, শুধু সংখ্যা)।"


# ---------------- এন্ট্রি — Auto vs Manual বেছে নেওয়া ----------------

async def _render_deposit_choice(session):
    config = await get_mraipay_config(session)
    return "💰 <b>কীভাবে Deposit করতে চান?</b>", deposit_choice_kb(auto_available=config is not None and bool(PUBLIC_BASE_URL))


@router.callback_query(F.data == "deposit")
async def cb_deposit_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with get_session() as session:
        text, kb = await _render_deposit_choice(session)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


# ---------------- ⚡ AUTO (Mr Ai Pay — bKash/Nagad/Rocket) ----------------

@router.callback_query(F.data == "dep_auto")
async def cb_dep_auto(call: CallbackQuery, state: FSMContext):
    async with get_session() as session:
        config = await get_mraipay_config(session)

    if config is None or not PUBLIC_BASE_URL:
        await call.answer("⚡ এই মুহূর্তে Auto Deposit available নেই।", show_alert=True)
        return

    await state.set_state(AutoDepositFlow.waiting_amount)
    await call.message.edit_text(
        "⚡ <b>Instant Deposit</b>\n\nআপনি কত টাকা Deposit করতে চান?\n\n(শুধু সংখ্যা লিখুন, যেমন: 500)",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AutoDepositFlow.waiting_amount, F.text)
async def auto_deposit_amount_entered(message: Message, state: FSMContext):
    amount = parse_amount(message.text, MIN_DEPOSIT, MAX_DEPOSIT)
    if amount is None:
        await message.answer(_AMOUNT_HINT)
        return

    async with get_session() as session:
        config = await get_mraipay_config(session)
        user = await get_or_create_user(session, message.from_user)
        await session.commit()

    if config is None or not PUBLIC_BASE_URL:
        await message.answer("⚡ এই মুহূর্তে Auto Deposit available নেই। Manual দিয়ে ট্রাই করুন।")
        await state.clear()
        return

    wait_msg = await message.answer("⏳ Payment link তৈরি করা হচ্ছে...")

    client = MrAiPayClient(config["api_key"], config["secret_key"], config["brand_key"])
    try:
        link = await client.create_payment(
            amount=amount,
            cus_name=message.from_user.first_name or "Customer",
            cus_email=f"user{user.id}@telegram.local",
            success_url=f"{PUBLIC_BASE_URL}/payment/success",
            cancel_url=f"{PUBLIC_BASE_URL}/payment/cancel",
            metadata={"tg_user_id": user.id},
        )
    except MrAiPayError as exc:
        logger.warning("Mr Ai Pay create failed: %s", exc)
        await wait_msg.edit_text("❌ Payment link তৈরি করা যায়নি। একটু পরে আবার চেষ্টা করুন, অথবা Manual Deposit ব্যবহার করুন।")
        await notify_admins(message.bot, f"⚠️ Mr Ai Pay payment create failed: {esc(str(exc))[:300]}")
        await state.clear()
        return

    # Remember the attempt: if the gateway told us its transaction id, a background job
    # will credit the user even if they close the browser before the redirect.
    async with get_session() as session:
        session.add(PaymentIntent(user_id=user.id, amount=amount, gateway_txn_id=link.transaction_id, status="CREATED"))
        await session.commit()

    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="💳 Pay Now", web_app=WebAppInfo(url=link.url))
    b.button(text="🌐 ব্রাউজারে খুলুন", url=link.url)  # fallback if the in-app web view misbehaves
    b.adjust(1)
    await wait_msg.edit_text(
        f"⚡ <b>Instant Deposit</b>\n\n💰 Amount: {fmt_money(amount)}\n\n"
        "নিচের বাটনে চেপে bKash/Nagad/Rocket দিয়ে পে করুন। পেমেন্ট সম্পন্ন হলে "
        "আপনার ব্যালেন্স <b>স্বয়ংক্রিয়ভাবে</b> যোগ হয়ে যাবে এবং একটা নিশ্চিতকরণ মেসেজ পাবেন।\n\n"
        "ℹ️ পেমেন্টের পর পেজটা বন্ধ করবেন না — \"Payment Successful\" দেখা পর্যন্ত অপেক্ষা করুন।",
        reply_markup=b.as_markup(),
    )


# ---------------- 🔄 RECOVERY — paid, but closed the page before the redirect ----------------

@router.callback_query(F.data == "dep_recover")
async def cb_dep_recover(call: CallbackQuery, state: FSMContext):
    async with get_session() as session:
        config = await get_mraipay_config(session)
    if config is None:
        await call.answer("⚡ এই মুহূর্তে Auto Deposit বন্ধ আছে।", show_alert=True)
        return
    await state.set_state(AutoDepositFlow.waiting_recover_txn)
    await call.message.edit_text(
        "🔄 <b>Payment Verify</b>\n\nপেমেন্টের <b>Transaction ID</b> লিখুন "
        "(পেমেন্ট পেজ বা SMS/রিসিপ্টে থাকে, দেখতে অনেকটা <code>OVKPXW165414</code>)। "
        "সেটা Mr Ai Pay-তে যাচাই করে সঠিক হলে ব্যালেন্স যোগ করা হবে।\n\n"
        "ID না পেলে সাপোর্টে যোগাযোগ করুন — Admin Mr Ai Pay ড্যাশবোর্ড থেকে ID দেখে দিতে পারবেন।",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(AutoDepositFlow.waiting_recover_txn, F.text)
async def recover_txn_entered(message: Message, state: FSMContext):
    txn_id = message.text.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{4,128}", txn_id):
        await message.answer("⚠️ সঠিক Transaction ID লিখুন (শুধু অক্ষর ও সংখ্যা)।")
        return

    # one check at a time per user (every attempt calls the gateway)
    async with get_lock("recover", message.from_user.id):
        async with get_session() as session:
            config = await get_mraipay_config(session)
            if config is None:
                await message.answer("⚡ এই মুহূর্তে Auto Deposit বন্ধ আছে।")
                await state.clear()
                return
            client = MrAiPayClient(config["api_key"], config["secret_key"], config["brand_key"])
            try:
                outcome = await verify_and_record(session, client, txn_id)
            except MrAiPayError as exc:
                logger.info("Recovery verify failed for %s: %s", txn_id, exc)
                await message.answer("❌ এই Transaction ID যাচাই করা যায়নি। ID আবার চেক করুন।")
                return

    if outcome.user_id is None:
        await message.answer("❌ এই Transaction ID-র সাথে কোনো অ্যাকাউন্ট মেলেনি।")
        return
    if outcome.user_id != message.from_user.id:
        # never reveal or credit someone else's payment
        await message.answer("❌ এই পেমেন্ট আপনার অ্যাকাউন্টের সাথে যুক্ত নয়।")
        return

    await state.clear()
    if outcome.status == "COMPLETED":
        if outcome.newly_credited:
            await message.answer(deposit_success_text(outcome.amount))
        else:
            await message.answer("ℹ️ এই পেমেন্ট আগেই আপনার ব্যালেন্সে যোগ করা হয়েছে। Wallet চেক করুন।")
    elif outcome.status == "PENDING":
        await message.answer("⏳ পেমেন্ট এখনো confirm হয়নি। একটু পরে আবার চেষ্টা করুন।")
    else:
        await message.answer("❌ এই পেমেন্ট সম্পন্ন হয়নি।")


# ---------------- ✍️ MANUAL ----------------

async def _render_deposit_methods(session):
    result = await session.execute(select(PaymentMethod).where(PaymentMethod.is_active == True))  # noqa: E712
    methods = result.scalars().all()
    if not methods:
        return "💰 বর্তমানে কোনো Deposit Method available নেই। সাপোর্টে যোগাযোগ করুন।", None
    return "✍️ <b>Select Deposit Method:</b>", deposit_methods_kb(methods)


@router.callback_query(F.data == "dep_manual")
async def cb_dep_manual(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with get_session() as session:
        text, kb = await _render_deposit_methods(session)
    await call.message.edit_text(text, reply_markup=kb or _NO_INLINE)
    await call.answer()


@router.callback_query(F.data.startswith("dep_method:"))
async def cb_deposit_method_selected(call: CallbackQuery, state: FSMContext):
    method_id = int(call.data.split(":")[1])
    async with get_session() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.id == method_id))
        method = result.scalar_one_or_none()

    if method is None or not method.is_active:
        await call.answer("❌ এই method টি আর available নেই।", show_alert=True)
        return

    await state.update_data(method_id=method_id, method_name=method.method_name)
    await state.set_state(DepositFlow.waiting_amount)
    await call.message.edit_text(
        "আপনি কত টাকা Deposit করতে চান?\n\n(শুধু সংখ্যা লিখুন, যেমন: 500)",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(DepositFlow.waiting_amount, F.text)
async def deposit_amount_entered(message: Message, state: FSMContext):
    amount = parse_amount(message.text, MIN_DEPOSIT, MAX_DEPOSIT)
    if amount is None:
        await message.answer(_AMOUNT_HINT)
        return

    data = await state.get_data()
    async with get_session() as session:
        result = await session.execute(select(PaymentMethod).where(PaymentMethod.id == data["method_id"]))
        method = result.scalar_one_or_none()

    if method is None or not method.is_active:
        await message.answer("❌ Method not found. আবার শুরু করুন।")
        await state.clear()
        return

    await state.update_data(amount=str(amount))  # str: FSM data must stay JSON-serialisable (Redis storage)
    await state.set_state(DepositFlow.waiting_txn_id)
    await message.answer(
        f"💰 Amount: {fmt_money(amount)}\n\n"
        f"📱 Send Money to ({esc(method.method_name)}):\n<code>{esc(method.account_number)}</code>\n\n"
        f"{esc(method.instructions)}\n\n"
        "টাকা পাঠানোর পর, <b>Transaction ID</b> লিখে পাঠান:",
        reply_markup=cancel_kb(),
    )


@router.message(DepositFlow.waiting_txn_id, F.text)
async def deposit_txn_id_entered(message: Message, state: FSMContext):
    txn_id = normalize_txn_id(message.text)
    if txn_id is None:
        await message.answer("⚠️ সঠিক Transaction ID লিখুন (শুধু অক্ষর ও সংখ্যা, ৬–৩২ অক্ষর)।")
        return

    async with get_session() as session:
        if await is_duplicate_txn(session, txn_id):
            await message.answer("⚠️ এই Transaction ID আগেই জমা দেওয়া হয়েছে। সঠিক ID আছে কিনা আবার দেখুন।")
            return

    await state.update_data(transaction_id=txn_id)
    await state.set_state(DepositFlow.waiting_sender_number)
    await message.answer("📱 আপনার Sender Mobile Number লিখুন:", reply_markup=cancel_kb())


@router.message(DepositFlow.waiting_sender_number, F.text)
async def deposit_sender_number_entered(message: Message, state: FSMContext):
    sender_number = parse_phone(message.text)
    if sender_number is None:
        await message.answer("⚠️ সঠিক Mobile Number লিখুন (যেমন: 01712345678)।")
        return

    data = await state.get_data()
    amount = Decimal(data["amount"])
    async with get_session() as session:
        user = await get_or_create_user(session, message.from_user)
        await session.commit()
        try:
            deposit = await create_deposit_request(
                session, user, data["method_id"], amount, data["transaction_id"], sender_number,
            )
        except DuplicateTransactionError:
            await message.answer("⚠️ এই Transaction ID আগেই জমা দেওয়া হয়েছে।")
            await state.clear()
            return
        except TooManyPendingError:
            await message.answer(
                "⏳ আপনার একাধিক Deposit এখনো Pending আছে। Admin রিভিউ করার পর নতুন request দিন।"
            )
            await state.clear()
            return

    await state.clear()
    await message.answer(
        "✅ <b>Deposit Request তৈরি হয়েছে!</b>\n\n"
        f"💰 Amount: {fmt_money(amount)}\n"
        f"💳 Method: {esc(data['method_name'])}\n"
        f"🆔 Transaction ID: {esc(deposit.transaction_id)}\n\n"
        "🟡 Status: Pending Review\n\n"
        "Admin আপনার deposit রিভিউ করার পর ব্যালেন্স যোগ হবে।",
    )

    await notify_admins(message.bot, (
        "💰 <b>New Deposit Request</b>\n\n"
        f"👤 {esc(message.from_user.first_name)} (<code>{message.from_user.id}</code>)\n"
        f"💳 Method: {esc(data['method_name'])}\n"
        f"💰 Amount: {fmt_money(amount)}\n"
        f"🆔 Txn ID: {esc(deposit.transaction_id)}\n"
        f"📱 Sender: {esc(sender_number)}\n\n"
        f"Review via Admin Panel → 💰 Deposit Requests → #{deposit.id}"
    ))

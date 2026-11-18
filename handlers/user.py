from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from config import GAME_NAME, SERVER_LABEL, SUPPORT_USERNAME
from database.database import get_session
from database.models import Order, Package, User
from keyboards.user import main_reply_kb, package_detail_kb, packages_kb
from services.wallet import get_or_create_user, get_or_create_user_ex
from utils.helpers import esc, fmt_money, parse_uid
from utils.states import OrderFlow, UidCheckFlow

router = Router(name="user")

_NO_INLINE = InlineKeyboardMarkup(inline_keyboard=[])  # edit_text-এ পুরনো ইনলাইন বাটন মুছতে ব্যবহার হয়


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    async with get_session() as session:
        user, created = await get_or_create_user_ex(session, message.from_user)

        # Referral counts ONLY for brand-new accounts. (Before: any existing user could
        # attach a referrer later, and two users could refer each other to farm bonuses.)
        if created and command.args:
            result = await session.execute(select(User).where(User.referral_code == command.args.strip()))
            referrer = result.scalar_one_or_none()
            if referrer and referrer.id != user.id:
                user.referred_by = referrer.id

        await session.commit()
        banned = user.is_banned

    if banned:
        await message.answer("🚫 আপনার অ্যাকাউন্ট ব্যান করা হয়েছে। সাহায্যের জন্য সাপোর্টে যোগাযোগ করুন।")
        return

    text = (
        f"👋 <b>স্বাগতম, {esc(message.from_user.first_name)}!</b>\n\n"
        f"💎 <b>{GAME_NAME} Diamond Top-up</b> — {SERVER_LABEL}\n\n"
        "দ্রুত, নিরাপদ ও সহজে Diamond top-up করুন। নিচের মেনু থেকে শুরু করুন 👇"
    )
    await message.answer(text, reply_markup=main_reply_kb())


async def render_home(session, user: User) -> str:
    result = await session.execute(select(func.count(Order.id)).where(Order.user_id == user.id))
    total_orders = result.scalar() or 0
    return (
        f"👋 <b>স্বাগতম, {esc(user.first_name)}</b>\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n\n"
        f"💰 Wallet Balance: {fmt_money(user.balance)}\n\n"
        f"📦 Total Orders: {total_orders}\n\n"
        f"🎁 Total Referral Earnings: {fmt_money(user.referral_earnings)}"
    )


# ---------------- মূল নেভিগেশন handlers/nav.py-তে (সর্বোচ্চ অগ্রাধিকার router) ----------------

def _profile_text(user: User) -> str:
    return (
        f"👤 <b>Profile</b>\n\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"Username: @{esc(user.username) or '—'}\n"
        f"Name: {esc(user.first_name)}\n\n"
        f"💰 Balance: {fmt_money(user.balance)}\n"
        f"💵 মোট Deposit: {fmt_money(user.total_deposit)}\n"
        f"🛒 মোট খরচ: {fmt_money(user.total_spent)}\n"
        f"🎁 Referral আয়: {fmt_money(user.referral_earnings)}\n\n"
        f"📅 যোগদান: {user.created_at.strftime('%Y-%m-%d')}"
    )


def _wallet_text(user: User) -> str:
    return (
        f"💰 <b>Wallet</b>\n\n"
        f"Balance: {fmt_money(user.balance)}\n"
        f"মোট Deposit: {fmt_money(user.total_deposit)}\n"
        f"মোট খরচ: {fmt_money(user.total_spent)}\n\n"
        "টাকা যোগ করতে নিচের ➕ Deposit বাটনে চাপুন।"
    )


# Defensive callbacks (পুরনো ইনলাইন বাটন থেকে callback এলেও যেন crash না করে)

@router.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with get_session() as session:
        user = await get_or_create_user(session, call.from_user)
        await session.commit()
        text = await render_home(session, user)
    await call.message.edit_text(text, reply_markup=_NO_INLINE)
    await call.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    async with get_session() as session:
        user = await get_or_create_user(session, call.from_user)
        await session.commit()
    await call.message.edit_text(_profile_text(user), reply_markup=_NO_INLINE)
    await call.answer()


@router.callback_query(F.data == "wallet")
async def cb_wallet(call: CallbackQuery):
    async with get_session() as session:
        user = await get_or_create_user(session, call.from_user)
        await session.commit()
    await call.message.edit_text(_wallet_text(user), reply_markup=_NO_INLINE)
    await call.answer()


@router.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await call.message.edit_text(
        f"❓ <b>Help / Support</b>\n\nযেকোনো সমস্যায় যোগাযোগ করুন: {esc(SUPPORT_USERNAME)}",
        reply_markup=_NO_INLINE,
    )
    await call.answer()


# ---------------- TOP-UP FLOW ----------------

async def _render_topup_menu(session) -> tuple[str, object]:
    result = await session.execute(select(Package).where(Package.is_active == True).order_by(Package.price))  # noqa: E712
    packages = result.scalars().all()
    if not packages:
        return "💎 বর্তমানে কোনো Package available নেই। একটু পরে আবার চেষ্টা করুন।", _NO_INLINE
    text = f"💎 <b>{GAME_NAME} Diamond Top-up</b> — {SERVER_LABEL}\n\nএকটা Package বেছে নিন:"
    return text, packages_kb(packages)


@router.callback_query(F.data == "topup_menu")
async def cb_topup_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    async with get_session() as session:
        text, kb = await _render_topup_menu(session)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("pkg:"))
async def cb_package_detail(call: CallbackQuery, state: FSMContext):
    pkg_id = int(call.data.split(":")[1])
    async with get_session() as session:
        result = await session.execute(select(Package).where(Package.id == pkg_id))
        pkg = result.scalar_one_or_none()
        user = await get_or_create_user(session, call.from_user)
        await session.commit()
        has_saved_id = bool(user.last_player_id)
        saved_id = user.last_player_id

    if pkg is None or not pkg.is_active:
        await call.answer("❌ এই package টি আর available নেই।", show_alert=True)
        return

    await state.update_data(pkg_id=pkg.id)
    text = (
        f"💎 <b>{esc(pkg.name)}</b>\n\n"
        f"💰 Price: {fmt_money(pkg.price)}\n"
        f"🌍 Server: {SERVER_LABEL}\n\n"
        "আপনার Free Fire Player ID (UID) প্রয়োজন।"
    )
    if has_saved_id:
        text += f"\n\nআপনার আগের ID: <code>{esc(saved_id)}</code>"

    await call.message.edit_text(text, reply_markup=package_detail_kb(pkg.id, has_saved_id))
    await call.answer()


@router.callback_query(F.data.startswith("pkg_enter_id:"))
async def cb_pkg_enter_id(call: CallbackQuery, state: FSMContext):
    pkg_id = int(call.data.split(":")[1])
    await state.update_data(pkg_id=pkg_id)
    await state.set_state(OrderFlow.waiting_player_id)
    await call.message.edit_text(
        "✏️ আপনার Free Fire Player ID (UID) লিখে পাঠান:\n\n"
        "⚠️ শুধু সংখ্যা, in-game প্রোফাইলে গিয়ে দেখে নিন সঠিক ID।",
        reply_markup=_NO_INLINE,
    )
    await call.answer()


@router.message(OrderFlow.waiting_player_id, F.text)
async def player_id_entered(message: Message, state: FSMContext):
    player_id = parse_uid(message.text)
    if player_id is None:
        await message.answer("⚠️ সঠিক Player ID লিখুন (শুধু সংখ্যা, ৬–১৪ ডিজিট, in-game UID)।")
        return
    await _show_confirmation(message, state, player_id)


@router.callback_query(F.data.startswith("pkg_use_saved:"))
async def cb_pkg_use_saved(call: CallbackQuery, state: FSMContext):
    pkg_id = int(call.data.split(":")[1])
    await state.update_data(pkg_id=pkg_id)
    async with get_session() as session:
        user = await get_or_create_user(session, call.from_user)
        await session.commit()
        player_id = user.last_player_id
    if not player_id:
        await call.answer("সেভ করা ID পাওয়া যায়নি, নতুন করে লিখুন।", show_alert=True)
        return
    await _show_confirmation(call.message, state, player_id, is_callback=True)
    await call.answer()


async def _show_confirmation(message: Message, state: FSMContext, player_id: str, is_callback: bool = False):
    from database.models import ApiProvider
    from keyboards.user import order_final_confirm_kb
    from services.api_client import ApiClientError, get_provider_client

    data = await state.get_data()
    pkg_id = data.get("pkg_id")
    async with get_session() as session:
        result = await session.execute(select(Package).where(Package.id == pkg_id))
        pkg = result.scalar_one_or_none()
        provider = None
        if pkg and pkg.api_provider_id:
            prov_result = await session.execute(select(ApiProvider).where(ApiProvider.id == pkg.api_provider_id))
            provider = prov_result.scalar_one_or_none()

    if pkg is None or not pkg.is_active:
        await message.answer("❌ এই package টি আর available নেই।")
        await state.clear()
        return

    await state.update_data(player_id=player_id)
    await state.set_state(OrderFlow.confirming)

    # Epinby হলে সম্ভব হলে nickname দেখানো হয় (optional — ব্যর্থ হলে চুপচাপ স্কিপ)।
    nickname_line = ""
    if provider and provider.provider_type == "EPINBY" and pkg.api_service_id:
        try:
            client = get_provider_client(provider)
            info = await client.validate_player(pkg.api_service_id, player_id)
            nickname = info.get("nickname") or info.get("username")
            if nickname:
                nickname_line = f"✅ In-game Name: <b>{esc(nickname)}</b>\n\n"
        except ApiClientError:
            pass

    text = (
        "🧾 <b>অর্ডার নিশ্চিত করুন</b>\n\n"
        f"💎 Package: {esc(pkg.name)}\n"
        f"💰 Price: {fmt_money(pkg.price)}\n"
        f"🌍 Server: {SERVER_LABEL}\n"
        f"🆔 Player ID: <code>{esc(player_id)}</code>\n\n"
        f"{nickname_line}"
        "⚠️ <b>আপনার Player ID টা আরেকবার ভালো করে চেক করুন।</b>\n"
        "ভুল ID-তে টপ-আপ হয়ে গেলে তা ফেরত/রিফান্ড করা সম্ভব না।"
    )
    # The price is embedded in the button: if the admin changes it before the
    # customer confirms, the order is refused instead of silently charging more.
    kb = order_final_confirm_kb(pkg.id, pkg.price)
    if is_callback:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


# ---------------- UID CHECK ----------------

async def _find_epinby_lookup_package(session):
    """যেকোনো Active, Epinby-linked package থেকে validate-player-এর product_id নেওয়া হয়।"""
    from database.models import ApiProvider

    result = await session.execute(
        select(Package, ApiProvider)
        .join(ApiProvider, Package.api_provider_id == ApiProvider.id)
        .where(
            Package.is_active == True,  # noqa: E712
            ApiProvider.is_active == True,  # noqa: E712
            ApiProvider.provider_type == "EPINBY",
            Package.api_service_id.is_not(None),
        )
        .limit(1)
    )
    return result.first()


@router.message(UidCheckFlow.waiting_uid, F.text)
async def uid_check_entered(message: Message, state: FSMContext):
    from services.api_client import ApiClientError, get_provider_client

    uid = parse_uid(message.text)
    if uid is None:
        await message.answer("⚠️ সঠিক UID লিখুন (শুধু সংখ্যা, ৬–১৪ ডিজিট, in-game UID)।")
        return

    async with get_session() as session:
        row = await _find_epinby_lookup_package(session)

    if row is None:
        await message.answer("🔍 এই মুহূর্তে UID Check ফিচারটি সেটআপ করা নেই।")
        await state.clear()
        return

    pkg, provider = row
    checking_msg = await message.answer("🔎 চেক করা হচ্ছে...")

    try:
        client = get_provider_client(provider)
        info = await client.validate_player(pkg.api_service_id, uid)
        nickname = info.get("nickname") or info.get("username")
        if nickname:
            await checking_msg.edit_text(
                f"✅ <b>পাওয়া গেছে!</b>\n\n🆔 UID: <code>{esc(uid)}</code>\n🎮 In-game Name: <b>{esc(nickname)}</b>",
            )
        else:
            await checking_msg.edit_text(
                f"⚠️ এই UID-এর জন্য কোনো নাম পাওয়া যায়নি। UID-টা আরেকবার চেক করুন।\n\n🆔 UID: <code>{esc(uid)}</code>",
            )
    except ApiClientError:
        await checking_msg.edit_text(
            f"❌ এই UID টা খুঁজে পাওয়া যায়নি বা সার্ভার ব্যস্ত আছে। UID-টা সঠিক কিনা আবার চেক করুন।\n\n🆔 UID: <code>{esc(uid)}</code>",
        )

    await state.clear()

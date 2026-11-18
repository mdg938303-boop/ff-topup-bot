"""
Admin: import packages straight from a provider's catalog (Epinby).

  Provider -> 📥 Import Packages
     1. type a game name ("free fire")  -> matching games (or categories) of the provider
     2. pick the game -> its categories (⭐ Bangladesh first) -> pick the category
     3. tick the products you want (or "select all")
     4. exchange rate (asked once) + your profit ("10%" or "15")
     5. preview with cost / selling price / profit  -> ✅ Import (all packages go live, ACTIVE)

Imported packages keep following the provider's price (services/catalog.py price sync).
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import ADMIN_IDS
from database.database import SessionLocal, get_session
from database.models import ActivityLog, ApiProvider, Package
from services.api_client import ApiClientError, get_provider_client
from services.catalog import get_rate, import_products, set_rate, sync_prices
from services.pricing import compute_sell_price, cost_in_bdt, markup_label, parse_markup, parse_rate
from utils.helpers import esc, fmt_money
from utils.states import AdminImportFlow

logger = logging.getLogger(__name__)
router = Router(name="admin_import")

PAGE = 8
MAX_PRODUCTS = 300
_BD_RE = re.compile(r"bangladesh|\bbd\b", re.IGNORECASE)


def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9\u0980-\u09ff]+", "", (text or "").lower())


def _fmt_cost(value, currency: str) -> str:
    return f"{Decimal(str(value)).normalize():f} {currency}"


async def _provider(provider_id: int | None) -> ApiProvider | None:
    if not provider_id:
        return None
    async with get_session() as session:
        return (await session.execute(select(ApiProvider).where(ApiProvider.id == provider_id))).scalar_one_or_none()


def _back_kb(provider_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Back", callback_data=f"adm:prov:{provider_id}")
    return b.as_markup()


# ----------------------------------------------------------------------
# 1. start + search
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:imp:"))
async def cb_import_start(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    provider_id = int(call.data.split(":")[2])
    provider = await _provider(provider_id)
    if provider is None:
        await call.answer("Provider পাওয়া যায়নি।", show_alert=True)
        return
    if provider.provider_type != "EPINBY":
        await call.answer("ℹ️ Catalog import এখন শুধু Epinby provider-এ চলে।", show_alert=True)
        return

    await state.clear()
    await state.update_data(provider_id=provider.id, currency=provider.currency or "USD")
    await state.set_state(AdminImportFlow.waiting_game)
    await call.message.edit_text(
        "📥 <b>Import Packages</b>\n\nযে গেমের প্যাকেজ আনতে চান তার নাম লিখুন\n(যেমন: <code>free fire</code>):",
        reply_markup=_back_kb(provider.id),
    )
    await call.answer()


@router.message(AdminImportFlow.waiting_game, F.text)
async def import_game_search(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    provider = await _provider(data.get("provider_id"))
    if provider is None:
        await state.clear()
        await message.answer("মেয়াদ শেষ। আবার শুরু করুন।")
        return

    query = message.text.strip()
    wait = await message.answer("🔎 খোঁজা হচ্ছে...")
    client = get_provider_client(provider)
    try:
        games = await client.list_games()
        key = _norm(query)
        matches = [g for g in games if key and key in _norm(g["name"])][:12]

        b = InlineKeyboardBuilder()
        if matches:
            await state.update_data(games={str(g["id"]): g["name"] for g in matches})
            for g in matches:
                b.button(text=f"🎮 {g['name']}", callback_data=f"adm:imp_game:{g['id']}")
            title = f"🎮 <b>{len(matches)}</b> টি গেম পাওয়া গেছে — একটা বেছে নিন:"
        else:
            # Some catalogs list regions ("Freefire Bangladesh") as categories, not games -> search products too
            found = await client.list_products(game=query, max_pages=2) or await client.list_products(category=query, max_pages=2)
            groups: dict[tuple[int, int], list] = {}
            for p in found:
                groups.setdefault((p["game_id"], p["category_id"]), []).append(p)
            if not groups:
                await wait.edit_text("❌ কিছু পাওয়া যায়নি। অন্য নামে (যেমন <code>freefire</code>) আবার লিখুন:")
                return
            ordered = sorted(groups.items(), key=lambda kv: (not _BD_RE.search(kv[1][0]["category"]), -len(kv[1])))[:10]
            for (game_id, cat_id), items in ordered:
                star = "⭐ " if _BD_RE.search(items[0]["category"]) else ""
                label = f"{star}{items[0]['game'] or '—'} › {items[0]['category'] or '—'} ({len(items)})"
                b.button(text=label[:60], callback_data=f"adm:imp_cat:{game_id}:{cat_id}")
            title = "📂 এই ক্যাটাগরিগুলো পাওয়া গেছে — একটা বেছে নিন:"
    except ApiClientError as exc:
        await wait.edit_text(f"❌ Provider থেকে তথ্য আনা যায়নি: {esc(str(exc))[:200]}\n\nAPI key ঠিক আছে কিনা দেখুন।")
        return

    b.button(text="⬅️ Back", callback_data=f"adm:prov:{provider.id}")
    b.adjust(1)
    await wait.edit_text(f"{title}\n\n(অন্য নাম লিখলে আবার সার্চ হবে)", reply_markup=b.as_markup())


# ----------------------------------------------------------------------
# 2. game -> categories
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:imp_game:"))
async def cb_pick_game(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    game_id = int(call.data.split(":")[2])
    data = await state.get_data()
    provider = await _provider(data.get("provider_id"))
    if provider is None:
        await call.answer("মেয়াদ শেষ। আবার শুরু করুন।", show_alert=True)
        return

    try:
        categories = await get_provider_client(provider).list_categories(game_id)
    except ApiClientError as exc:
        await call.answer(f"❌ {str(exc)[:150]}", show_alert=True)
        return

    game_name = (data.get("games") or {}).get(str(game_id), "")
    await state.update_data(game_name=game_name)

    b = InlineKeyboardBuilder()
    b.button(text="📦 সব ক্যাটাগরি একসাথে", callback_data=f"adm:imp_cat:{game_id}:0")
    for c in sorted(categories, key=lambda c: (not _BD_RE.search(c["name"]), c["name"].lower()))[:30]:
        star = "⭐ " if _BD_RE.search(c["name"]) else ""
        b.button(text=f"{star}{c['name']}"[:60], callback_data=f"adm:imp_cat:{game_id}:{c['id']}")
    b.button(text="⬅️ Back", callback_data=f"adm:prov:{provider.id}")
    b.adjust(1)
    await call.message.edit_text(
        f"🎮 <b>{esc(game_name)}</b>\n\nকোন সার্ভার/ক্যাটাগরির প্যাকেজ আনবেন? (⭐ = Bangladesh)",
        reply_markup=b.as_markup(),
    )
    await call.answer()


# ----------------------------------------------------------------------
# 3. category -> product selection
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:imp_cat:"))
async def cb_pick_category(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    _, _, game_raw, cat_raw = call.data.split(":")   # adm : imp_cat : <game> : <category>
    game_id, cat_id = int(game_raw), int(cat_raw)
    data = await state.get_data()
    provider = await _provider(data.get("provider_id"))
    if provider is None:
        await call.answer("মেয়াদ শেষ। আবার শুরু করুন।", show_alert=True)
        return

    await call.answer("⏳ প্রোডাক্ট আনা হচ্ছে...")
    try:
        products = await get_provider_client(provider).list_products(
            game_id=game_id or None, category_id=cat_id or None, max_pages=5,
        )
    except ApiClientError as exc:
        await call.message.answer(f"❌ প্রোডাক্ট আনা যায়নি: {esc(str(exc))[:200]}")
        return
    if not products:
        await call.message.answer("❌ এই ক্যাটাগরিতে কোনো top-up প্রোডাক্ট পাওয়া যায়নি।")
        return

    products = sorted(products, key=lambda p: p["price"])[:MAX_PRODUCTS]
    first = products[0]
    title = f"{data.get('game_name') or first['game'] or '—'} › {first['category'] if cat_id else 'সব ক্যাটাগরি'}"
    await state.update_data(
        products=[
            {"id": p["id"], "name": p["name"], "price": str(p["price"]),
             "game_id": p["game_id"], "category_id": p["category_id"]}
            for p in products
        ],
        selected=[], page=0, game_id=game_id, category_id=cat_id, title=title,
    )
    await state.set_state(AdminImportFlow.browsing)
    await _render_selection(call, state)


async def _render_selection(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    products = data.get("products") or []
    selected = set(data.get("selected") or [])
    page = max(0, min(int(data.get("page") or 0), max(0, (len(products) - 1) // PAGE)))
    currency = data.get("currency") or "USD"
    chunk = products[page * PAGE:(page + 1) * PAGE]

    async with get_session() as session:
        existing = set((await session.execute(
            select(Package.api_service_id).where(
                Package.api_provider_id == data.get("provider_id"),
                Package.api_service_id.in_([str(p["id"]) for p in chunk]),
            )
        )).scalars().all())

    b = InlineKeyboardBuilder()
    for offset, p in enumerate(chunk):
        index = page * PAGE + offset
        mark = "☑️" if p["id"] in selected else "⬜"
        have = " ✔" if str(p["id"]) in existing else ""
        b.button(text=f"{mark} {p['name'][:30]} · {_fmt_cost(p['price'], currency)}{have}", callback_data=f"adm:imp_t:{index}")
    nav = []
    if page > 0:
        nav.append(("⬅️", f"adm:imp_p:{page - 1}"))
    if (page + 1) * PAGE < len(products):
        nav.append(("➡️", f"adm:imp_p:{page + 1}"))
    for text, cb in nav:
        b.button(text=text, callback_data=cb)
    b.button(text="✅ সব সিলেক্ট", callback_data="adm:imp_all")
    b.button(text="🧹 ক্লিয়ার", callback_data="adm:imp_none")
    b.button(text=f"➡️ পরের ধাপ: লাভ সেট ({len(selected)})", callback_data="adm:imp_next")
    b.button(text="⬅️ Back", callback_data=f"adm:prov:{data.get('provider_id')}")
    rows = [1] * len(chunk) + ([len(nav)] if nav else []) + [2, 1, 1]
    b.adjust(*rows)

    text = (
        f"📥 <b>{esc(data.get('title'))}</b>\n\n"
        f"প্রোডাক্ট: {len(products)} · সিলেক্টেড: <b>{len(selected)}</b> · পৃষ্ঠা {page + 1}/{max(1, (len(products) + PAGE - 1) // PAGE)}\n"
        "ট্যাপ করে বেছে নিন। ✔ = shop-এ আগে থেকেই আছে (আবার import করলে দাম আপডেট হবে)।"
    )
    await call.message.edit_text(text, reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("adm:imp_t:"))
async def cb_toggle(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    products = data.get("products") or []
    index = int(call.data.split(":")[2])
    if not (0 <= index < len(products)):
        await call.answer("মেয়াদ শেষ।", show_alert=True)
        return
    pid = products[index]["id"]
    selected = list(data.get("selected") or [])
    selected.remove(pid) if pid in selected else selected.append(pid)
    await state.update_data(selected=selected)
    await _render_selection(call, state)
    await call.answer()


@router.callback_query(F.data.startswith("adm:imp_p:"))
async def cb_page(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await state.update_data(page=int(call.data.split(":")[2]))
    await _render_selection(call, state)
    await call.answer()


@router.callback_query(F.data.in_({"adm:imp_all", "adm:imp_none"}))
async def cb_select_all(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    ids = [p["id"] for p in (data.get("products") or [])] if call.data == "adm:imp_all" else []
    await state.update_data(selected=ids)
    await _render_selection(call, state)
    await call.answer()


# ----------------------------------------------------------------------
# 4. exchange rate + profit
# ----------------------------------------------------------------------
@router.callback_query(F.data == "adm:imp_next")
async def cb_next(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    if not data.get("selected"):
        await call.answer("কমপক্ষে একটা প্রোডাক্ট সিলেক্ট করুন।", show_alert=True)
        return
    await _ask_rate_or_markup(call.message, state)
    await call.answer()


async def _ask_rate_or_markup(message: Message, state: FSMContext, edit: bool = True) -> None:
    data = await state.get_data()
    currency = data.get("currency") or "USD"
    async with get_session() as session:
        rate = await get_rate(session, currency)

    send = message.edit_text if edit else message.answer
    if rate is None:
        await state.set_state(AdminImportFlow.waiting_rate)
        await send(
            f"💱 <b>Exchange Rate</b>\n\nProvider-এর দাম {esc(currency)}-এ। ১ {esc(currency)} = কত টাকা?\n"
            "(যেমন: <code>125</code>) — এটা একবার সেভ হবে, পরে বদলানো যাবে।"
        )
        return
    await state.set_state(AdminImportFlow.waiting_markup)
    b = InlineKeyboardBuilder()
    b.button(text="💱 রেট বদলান", callback_data="adm:imp_rate")
    b.button(text="❌ বাতিল", callback_data="adm:imp_cancel")
    b.adjust(1)
    await send(
        f"💰 <b>আপনার লাভ কত যোগ করবেন?</b>\n\nবর্তমান রেট: ১ {esc(currency)} = ৳{rate.normalize():f}\n\n"
        "লিখুন:\n• <code>10%</code> — খরচের ওপর শতাংশ\n• <code>15</code> — প্রতি প্যাকেজে ফিক্সড ৳15",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data == "adm:imp_rate")
async def cb_change_rate(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    await state.set_state(AdminImportFlow.waiting_rate)
    await call.message.edit_text(f"💱 ১ {esc(data.get('currency') or 'USD')} = কত টাকা? (যেমন: <code>125</code>)")
    await call.answer()


@router.message(AdminImportFlow.waiting_rate, F.text)
async def import_rate_entered(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    rate = parse_rate(message.text)
    if rate is None:
        await message.answer("⚠️ সঠিক রেট লিখুন (০ এর বেশি সংখ্যা, যেমন 125)।")
        return
    data = await state.get_data()
    async with get_session() as session:
        await set_rate(session, data.get("currency"), rate)
        session.add(ActivityLog(actor_id=message.from_user.id, action="SET_RATE", details=f"{data.get('currency')}={rate}"))
        await session.commit()

    if data.get("rate_only"):
        await state.clear()
        wait = await message.answer("✅ রেট সেভ হয়েছে। ⏳ দাম আপডেট করা হচ্ছে...")
        report = await sync_prices(SessionLocal)
        await wait.edit_text(f"✅ রেট: ১ {esc(data.get('currency'))} = ৳{rate.normalize():f}\n\n{report.to_text()}",
                             reply_markup=_back_kb(data.get("provider_id")))
        return
    await _ask_rate_or_markup(message, state, edit=False)


@router.message(AdminImportFlow.waiting_markup, F.text)
async def import_markup_entered(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    parsed = parse_markup(message.text)
    if parsed is None:
        await message.answer("⚠️ সঠিক লাভ লিখুন — যেমন <code>10%</code> অথবা <code>15</code> (ঋণাত্মক নয়)।")
        return
    markup_type, markup_value = parsed
    data = await state.get_data()
    currency = data.get("currency") or "USD"
    async with get_session() as session:
        rate = await get_rate(session, currency)
    if rate is None:
        await _ask_rate_or_markup(message, state, edit=False)
        return

    await state.update_data(markup_type=markup_type, markup_value=str(markup_value))
    selected = set(data.get("selected") or [])
    chosen = [p for p in data.get("products", []) if p["id"] in selected]

    lines, total_profit = [], Decimal(0)
    for p in chosen:
        cost_bdt = cost_in_bdt(p["price"], rate)
        sell = compute_sell_price(p["price"], rate, markup_type, markup_value)
        total_profit += sell - cost_bdt
        if len(lines) < 15:
            lines.append(f"• {esc(p['name'][:34])}\n   {_fmt_cost(p['price'], currency)} ≈ {fmt_money(cost_bdt)} → <b>{fmt_money(sell)}</b> (লাভ {fmt_money(sell - cost_bdt)})")
    if len(chosen) > 15:
        lines.append(f"… আরও {len(chosen) - 15} টি")

    b = InlineKeyboardBuilder()
    b.button(text=f"✅ Import করুন ({len(chosen)} টি, সব Active)", callback_data="adm:imp_go")
    b.button(text="✏️ লাভ বদলান", callback_data="adm:imp_next")
    b.button(text="❌ বাতিল", callback_data="adm:imp_cancel")
    b.adjust(1)
    await message.answer(
        f"🧾 <b>প্রিভিউ</b> — রেট ৳{rate.normalize():f}, লাভ {markup_label(markup_type, markup_value)}\n\n"
        + "\n".join(lines)
        + f"\n\n📦 মোট {len(chosen)} টি প্যাকেজ · প্রতিটি বিক্রিতে গড় লাভ ≈ {fmt_money(total_profit / max(1, len(chosen)))}\n"
        "ℹ️ দাম প্রতি ১৫ মিনিটে provider-এর সাথে স্বয়ংক্রিয় মেলানো হবে।",
        reply_markup=b.as_markup(),
    )


# ----------------------------------------------------------------------
# 5. confirm / cancel
# ----------------------------------------------------------------------
@router.callback_query(F.data == "adm:imp_cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    provider_id = (await state.get_data()).get("provider_id")
    await state.clear()
    await call.message.edit_text("❌ Import বাতিল করা হয়েছে।", reply_markup=_back_kb(provider_id) if provider_id else None)
    await call.answer()


@router.callback_query(F.data == "adm:imp_go")
async def cb_confirm(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    data = await state.get_data()
    await state.clear()   # consume: a double tap can't import twice
    if not (data.get("selected") and data.get("markup_type")):
        await call.answer("মেয়াদ শেষ। আবার শুরু করুন।", show_alert=True)
        return
    provider = await _provider(data.get("provider_id"))
    if provider is None:
        await call.answer("Provider পাওয়া যায়নি।", show_alert=True)
        return

    selected = set(data["selected"])
    chosen = [p for p in data.get("products", []) if p["id"] in selected]
    async with get_session() as session:
        rate = await get_rate(session, provider.currency)
        if rate is None:
            await call.answer("রেট সেট নেই।", show_alert=True)
            return
        result = await import_products(
            session, provider, chosen, rate, data["markup_type"], Decimal(data["markup_value"]),
            data.get("game_id"), data.get("category_id"),
        )
        session.add(ActivityLog(
            actor_id=call.from_user.id, action="IMPORT_PACKAGES",
            details=f"{provider.name}: +{result.created} new, {result.updated} updated ({data.get('title')})",
        ))
        await session.commit()

    b = InlineKeyboardBuilder()
    b.button(text="💎 Packages দেখুন", callback_data="adm:pkgs")
    b.button(text="⬅️ Provider", callback_data=f"adm:prov:{provider.id}")
    b.adjust(1)
    await call.message.edit_text(
        f"✅ <b>Import সম্পন্ন!</b>\n\n🆕 নতুন: {result.created}\n♻️ আপডেট: {result.updated}\n\n"
        "সব প্যাকেজ এখন <b>Active</b> — ইউজাররা সরাসরি কিনতে পারবে।",
        reply_markup=b.as_markup(),
    )
    await call.answer()


# ----------------------------------------------------------------------
# provider-level shortcuts: change rate / sync prices now
# ----------------------------------------------------------------------
@router.callback_query(F.data.startswith("adm:rate:"))
async def cb_rate_only(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    provider = await _provider(int(call.data.split(":")[2]))
    if provider is None:
        await call.answer("Provider পাওয়া যায়নি।", show_alert=True)
        return
    async with get_session() as session:
        current = await get_rate(session, provider.currency)
    await state.clear()
    await state.update_data(provider_id=provider.id, currency=provider.currency or "USD", rate_only=True)
    await state.set_state(AdminImportFlow.waiting_rate)
    now = f"বর্তমান: ৳{current.normalize():f}\n" if current else ""
    await call.message.edit_text(
        f"💱 <b>Exchange Rate</b>\n\n{now}১ {esc(provider.currency)} = কত টাকা? (যেমন <code>125</code>)\n\n"
        "সেভ হলে সব auto-price প্যাকেজের দাম নতুন রেটে আপডেট হবে।",
        reply_markup=_back_kb(provider.id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("adm:syncnow:"))
async def cb_sync_now(call: CallbackQuery):
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    provider_id = int(call.data.split(":")[2])
    await call.answer("⏳ দাম মেলানো হচ্ছে...")
    report = await sync_prices(SessionLocal)
    await call.message.edit_text(report.to_text(), reply_markup=_back_kb(provider_id))

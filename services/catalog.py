"""
Provider catalog import + automatic price sync (Epinby).

  import_products()  — creates/updates Package rows from products the admin picked, with profit added
  sync_prices()      — every 15 min: re-reads provider prices and keeps selling prices in step

Safety rules of the price sync:
  * a cost jump/drop of more than 2× is NOT applied automatically (could be an API glitch) — admin is warned
  * a package is switched off only when the provider no longer lists it / is out of stock, and never
    more than half of a group at once (a half-empty API answer must not wipe the shop)
  * selling price is always rounded UP to a whole taka, so it can't fall below cost
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import ApiProvider, Package
from services.api_client import ApiClientError, get_provider_client
from services.notify import notify_admins
from services.payments import get_setting, set_setting
from services.pricing import compute_sell_price, extract_amount_label, parse_rate
from utils.helpers import esc, fmt_money, to_money

logger = logging.getLogger(__name__)

SPIKE_RATIO = Decimal("2")
_FOUR_PLACES = Decimal("0.0001")


# ----------------------------------------------------------------------
# exchange rate
# ----------------------------------------------------------------------
async def get_rate(session: AsyncSession, currency: str | None) -> Decimal | None:
    """BDT per 1 unit of the provider's currency (1 if the provider already prices in BDT)."""
    cur = (currency or "USD").strip().upper()
    if cur in ("BDT", "TK"):
        return Decimal(1)
    return parse_rate(await get_setting(session, f"rate_{cur}", ""))


async def set_rate(session: AsyncSession, currency: str | None, rate: Decimal) -> None:
    await set_setting(session, f"rate_{(currency or 'USD').strip().upper()}", str(rate))


# ----------------------------------------------------------------------
# import
# ----------------------------------------------------------------------
@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0


async def import_products(
    session: AsyncSession, provider: ApiProvider, products: list[dict], rate: Decimal,
    markup_type: str, markup_value: Decimal, fallback_game_id: int | None, fallback_category_id: int | None,
) -> ImportResult:
    """products: [{"id", "name", "price"(str/Decimal), "game_id", "category_id"}]  — every package ends up ACTIVE."""
    result = ImportResult()
    ids = [str(p["id"]) for p in products]
    existing = {
        pkg.api_service_id: pkg
        for pkg in (await session.execute(
            select(Package).where(Package.api_provider_id == provider.id, Package.api_service_id.in_(ids))
        )).scalars().all()
    }
    for p in products:
        cost = Decimal(str(p["price"])).quantize(_FOUR_PLACES)
        sell = compute_sell_price(cost, rate, markup_type, markup_value)
        pkg = existing.get(str(p["id"]))
        if pkg is None:
            pkg = Package(
                name=str(p["name"])[:128], diamond_amount=extract_amount_label(str(p["name"])),
                price=sell, is_active=True, api_provider_id=provider.id, api_service_id=str(p["id"]),
            )
            session.add(pkg)
            result.created += 1
        else:
            pkg.price = sell
            pkg.is_active = True
            result.updated += 1
        pkg.cost_price = to_money(cost)
        pkg.src_cost = cost
        pkg.auto_price = True
        pkg.markup_type = markup_type
        pkg.markup_value = to_money(markup_value)
        pkg.src_game_id = int(p.get("game_id") or fallback_game_id or 0) or None
        pkg.src_category_id = int(p.get("category_id") or fallback_category_id or 0) or None
    await session.flush()
    return result


# ----------------------------------------------------------------------
# price sync
# ----------------------------------------------------------------------
@dataclass
class SyncReport:
    checked: int = 0
    price_changes: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_news(self) -> bool:
        return bool(self.price_changes or self.deactivated or self.warnings)

    def to_text(self, limit: int = 25) -> str:
        lines = [f"🔄 <b>Price Sync</b> — {self.checked} টি package চেক করা হয়েছে"]
        if self.price_changes:
            lines.append(f"\n💱 <b>দাম আপডেট ({len(self.price_changes)}):</b>")
            lines += self.price_changes[:limit]
            if len(self.price_changes) > limit:
                lines.append(f"… আরও {len(self.price_changes) - limit} টি")
        if self.deactivated:
            lines.append(f"\n⛔ <b>বন্ধ করা হয়েছে ({len(self.deactivated)}):</b>")
            lines += self.deactivated[:limit]
        if self.warnings:
            lines.append("\n⚠️ <b>সতর্কতা:</b>")
            lines += self.warnings[:limit]
        if not self.has_news:
            lines.append("\nকোনো পরিবর্তন নেই ✅")
        return "\n".join(lines)


async def sync_prices(session_pool: async_sessionmaker) -> SyncReport:
    report = SyncReport()
    async with session_pool() as session:
        packages = (await session.execute(
            select(Package.id, Package.api_provider_id, Package.src_game_id, Package.src_category_id).where(
                Package.auto_price == True,  # noqa: E712
                Package.api_provider_id.is_not(None), Package.api_service_id.is_not(None),
            )
        )).all()

    groups: dict[tuple[int, int, int], list[int]] = {}
    for pkg_id, provider_id, game_id, category_id in packages:
        groups.setdefault((provider_id, game_id or 0, category_id or 0), []).append(pkg_id)

    for key, pkg_ids in groups.items():
        try:
            await _sync_group(session_pool, report, key, pkg_ids)
        except Exception:  # noqa: BLE001
            logger.exception("Price sync failed for group %s", key)
            report.warnings.append(f"• Provider #{key[0]}: sync ব্যর্থ (logs দেখুন)")
    return report


async def _sync_group(session_pool: async_sessionmaker, report: SyncReport, key: tuple[int, int, int], pkg_ids: list[int]) -> None:
    provider_id, game_id, category_id = key
    async with session_pool() as session:
        provider = (await session.execute(select(ApiProvider).where(ApiProvider.id == provider_id))).scalar_one_or_none()
        if provider is None or not provider.is_active or provider.provider_type != "EPINBY":
            return
        rate = await get_rate(session, provider.currency)
        if rate is None:
            report.warnings.append(f"• {esc(provider.name)}: {esc(provider.currency)} → ৳ রেট সেট করা নেই")
            return

        try:
            products = await get_provider_client(provider).list_products(
                game_id=game_id or None, category_id=category_id or None, max_pages=10,
            )
        except ApiClientError as exc:
            logger.warning("Price sync: catalog fetch failed for provider %s: %s", provider_id, exc)
            report.warnings.append(f"• {esc(provider.name)}: catalog আনা যায়নি ({esc(str(exc))[:80]})")
            return
        by_id = {p["id"]: p for p in products}
        narrow = bool(game_id or category_id)   # a whole-catalog fetch is capped, so absence proves nothing

        packages = (await session.execute(
            select(Package).where(Package.id.in_(pkg_ids)).execution_options(populate_existing=True)
        )).scalars().all()

        missing: list[Package] = []
        for pkg in packages:
            report.checked += 1
            try:
                product = by_id.get(int(pkg.api_service_id))
            except (TypeError, ValueError):
                continue
            if product is None or (
                product["stock_status"].upper() == "FINITE" and product["stock_count"] in (0, "0")
            ):
                missing.append(pkg)
                continue
            if not pkg.markup_type or pkg.markup_value is None:
                report.warnings.append(f"• {esc(pkg.name)}: লাভ (markup) সেট নেই — আবার Import করুন")
                continue

            new_cost = product["price"].quantize(_FOUR_PLACES)
            old_cost = Decimal(str(pkg.src_cost)) if pkg.src_cost is not None else None
            if old_cost and old_cost > 0 and (new_cost > old_cost * SPIKE_RATIO or new_cost * SPIKE_RATIO < old_cost):
                report.warnings.append(
                    f"• {esc(pkg.name)}: cost {old_cost.normalize():f} → {new_cost.normalize():f} "
                    "(২× এর বেশি পরিবর্তন — অটো আপডেট করা হয়নি, যাচাই করুন)"
                )
                continue

            new_sell = compute_sell_price(new_cost, rate, pkg.markup_type, pkg.markup_value)
            old_sell = to_money(pkg.price)
            if old_cost != new_cost or old_sell != new_sell:
                report.price_changes.append(
                    f"• {esc(pkg.name)}: cost {(old_cost or Decimal(0)).normalize():f} → {new_cost.normalize():f}, "
                    f"বিক্রয় {fmt_money(old_sell)} → {fmt_money(new_sell)}"
                )
                pkg.src_cost = new_cost
                pkg.cost_price = to_money(new_cost)
                pkg.price = new_sell

        if missing:
            safe = narrow and bool(products) and len(missing) <= max(1, len(packages) // 2)
            for pkg in missing:
                if safe:
                    if pkg.is_active:
                        pkg.is_active = False
                        report.deactivated.append(f"• {esc(pkg.name)} — provider-এ নেই / stock শেষ")
                else:
                    report.warnings.append(f"• {esc(pkg.name)}: provider-এর লিস্টে পাওয়া যায়নি (অটো বন্ধ করা হয়নি)")
        await session.commit()


async def price_sync_job(bot: Bot, session_pool: async_sessionmaker) -> None:
    """Scheduler entry point — only bothers the admins when something actually changed."""
    report = await sync_prices(session_pool)
    if report.has_news:
        await notify_admins(bot, report.to_text())

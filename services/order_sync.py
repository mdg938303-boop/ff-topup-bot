"""
Background job (APScheduler, every 60 s): checks every API order that is still
PROCESSING against its provider, then
  completed -> DELIVERED + notify customer
  failed    -> auto-refund + notify customer
  partial   -> NOT auto-refunded (customer may have received part): admin alerted
  other     -> just remember the provider's status
Also alerts admins once about orders stuck in PROCESSING for too long.

Every transition goes through services.orders (atomic claim), so it is safe even
if an admin presses Cancel at the same moment, or two bot instances overlap during
a Render zero-downtime deploy.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import ORDER_STUCK_MINUTES
from database.models import ApiProvider, Order, OrderStatus
from services.api_client import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_PARTIAL, STATUS_PROCESSING, ApiClientError, get_provider_client,
)
from services.notify import notify_admins, notify_user
from services.orders import cancel_and_refund, mark_delivered
from utils.helpers import esc, utcnow

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 5


async def sync_all_processing_orders(bot: Bot, session_pool: async_sessionmaker) -> None:
    async with session_pool() as session:
        order_ids = list((await session.execute(
            select(Order.id).where(Order.status == OrderStatus.PROCESSING, Order.api_order_id.is_not(None))
        )).scalars().all())

    if order_ids:
        semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

        async def _guarded(order_id: int) -> None:
            async with semaphore:
                try:
                    await _sync_one(bot, session_pool, order_id)
                except Exception:  # noqa: BLE001
                    logger.exception("Order %s sync crashed", order_id)

        await asyncio.gather(*(_guarded(i) for i in order_ids))

    try:
        await _alert_stuck_orders(bot, session_pool)
    except Exception:  # noqa: BLE001
        logger.exception("Stuck-order alert failed")


async def _sync_one(bot: Bot, session_pool: async_sessionmaker, order_id: int) -> None:
    async with session_pool() as session:
        order = (await session.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
        if order is None or order.status != OrderStatus.PROCESSING or not order.api_order_id or not order.api_provider_id:
            return
        provider = (await session.execute(
            select(ApiProvider).where(ApiProvider.id == order.api_provider_id)
        )).scalar_one_or_none()
        if provider is None or not provider.is_active:
            return

        try:
            result = await get_provider_client(provider).get_status(order.api_order_id)
        except ApiClientError as exc:
            logger.warning("Order %s status sync failed: %s", order.order_code, exc)
            return

        normalized = result.get("status", STATUS_PROCESSING)
        raw = (result.get("raw") or order.api_status or "")[:64]
        now = utcnow()

        if normalized == STATUS_COMPLETED:
            done = await mark_delivered(session, order.id, api_status=raw, last_api_sync=now)
            if done:
                await notify_user(bot, done.user_id, (
                    "🎉 <b>Top-up সম্পন্ন হয়েছে!</b>\n\n"
                    f"💎 {esc(done.package_name_snapshot)}\n"
                    f"🆔 Order: {esc(done.order_code)}\n\n"
                    "গেমে গিয়ে চেক করে দেখুন। ধন্যবাদ! 🎮"
                ))

        elif normalized == STATUS_FAILED:
            done = await cancel_and_refund(
                session, order.id, note_prefix="Auto-refund", api_status=raw, last_api_sync=now,
            )
            if done:
                await notify_user(bot, done.user_id, (
                    "🔴 <b>দুঃখিত, আপনার অর্ডার সম্পন্ন করা যায়নি।</b>\n\n"
                    f"🆔 Order: {esc(done.order_code)}\n"
                    "আপনার টাকা wallet-এ ফেরত দেওয়া হয়েছে।"
                ))

        elif normalized == STATUS_PARTIAL:
            first_time = not order.needs_review
            await session.execute(
                update(Order).where(Order.id == order.id, Order.status == OrderStatus.PROCESSING)
                .values(api_status=raw, last_api_sync=now, needs_review=True, admin_alerted=True)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            if first_time:
                await notify_admins(bot, (
                    "⚠️ <b>Partial delivery — manual action needed</b>\n\n"
                    f"🆔 {esc(order.order_code)} (Player <code>{esc(order.player_id)}</code>)\n"
                    f"Provider status: {esc(raw)}\n\n"
                    "Auto-refund করা হয়নি। Admin Panel → Orders থেকে সিদ্ধান্ত নিন।"
                ))

        else:
            await session.execute(
                update(Order).where(Order.id == order.id)
                .values(api_status=raw, last_api_sync=now)
                .execution_options(synchronize_session=False)
            )
            await session.commit()


async def _alert_stuck_orders(bot: Bot, session_pool: async_sessionmaker) -> None:
    cutoff = utcnow() - timedelta(minutes=ORDER_STUCK_MINUTES)
    async with session_pool() as session:
        stuck = (await session.execute(
            select(Order).where(
                Order.status == OrderStatus.PROCESSING, Order.created_at <= cutoff, Order.admin_alerted == False,  # noqa: E712
            ).limit(20)
        )).scalars().all()

        for order in stuck:
            claim = await session.execute(
                update(Order).where(Order.id == order.id, Order.admin_alerted == False)  # noqa: E712
                .values(admin_alerted=True).execution_options(synchronize_session=False)
            )
            await session.commit()
            if claim.rowcount == 1:
                await notify_admins(bot, (
                    "⏳ <b>Order stuck in PROCESSING</b>\n\n"
                    f"🆔 {esc(order.order_code)} — Player <code>{esc(order.player_id)}</code>\n"
                    f"💎 {esc(order.package_name_snapshot)}\n"
                    f"Provider order: {esc(order.api_order_id) or '—'} ({esc(order.api_status) or '—'})\n"
                    f"{ORDER_STUCK_MINUTES}+ মিনিট ধরে আটকে আছে — provider panel চেক করুন।"
                ))

"""
Shared logic for Mr Ai Pay deposits:
  * verify_and_record()          — used by the /payment/success web page
  * reconcile_pending_intents()  — background job: credits users who paid but closed
                                   the browser before the redirect happened
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from database.models import PaymentIntent, User
from services.mraipay_client import MrAiPayClient, MrAiPayError
from services.notify import notify_user
from services.payments import get_mraipay_config, record_auto_deposit_if_new
from utils.helpers import fmt_money, utcnow

logger = logging.getLogger(__name__)


@dataclass
class VerifyOutcome:
    status: str
    amount: Decimal
    user_id: int | None
    newly_credited: bool


def deposit_success_text(amount) -> str:
    return (
        "✅ <b>Deposit সফল হয়েছে!</b>\n\n"
        f"💰 Amount: {fmt_money(amount)}\n\n"
        "আপনার ব্যালেন্স যোগ হয়ে গেছে। Wallet চেক করুন।"
    )


def _to_decimal(value) -> Decimal:
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else Decimal("0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_metadata(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


async def verify_and_record(session: AsyncSession, client: MrAiPayClient, transaction_id: str) -> VerifyOutcome:
    """Asks the gateway, then credits idempotently. May raise MrAiPayError."""
    result = await client.verify_payment(transaction_id)
    status = str(result.get("status") or "").upper()
    amount = _to_decimal(result.get("amount"))
    metadata = _parse_metadata(result.get("metadata") or result.get("meta_data"))

    try:
        tg_user_id = int(metadata.get("tg_user_id"))
    except (TypeError, ValueError):
        tg_user_id = None

    if tg_user_id is None:
        # Fallback if the gateway dropped our metadata: the verify response also echoes cus_email,
        # and we always create it as  user<telegram-id>@telegram.local
        match = re.fullmatch(r"user(\d{1,15})@telegram\.local", str(result.get("cus_email") or ""))
        if match:
            tg_user_id = int(match.group(1))

    user = None
    if tg_user_id is not None:
        user = (await session.execute(select(User).where(User.id == tg_user_id))).scalar_one_or_none()
    if user is None:
        return VerifyOutcome(status, amount, None, False)

    newly = await record_auto_deposit_if_new(
        session, user, transaction_id, amount, result.get("payment_method"), status,
    )
    return VerifyOutcome(status, amount, user.id, newly)


async def reconcile_pending_intents(bot: Bot, session_pool: async_sessionmaker) -> None:
    """Runs every minute. Only helps for intents whose gateway transaction id we learned at creation."""
    async with session_pool() as session:
        config = await get_mraipay_config(session)
        if config is None:
            return
        now = utcnow()
        await session.execute(
            update(PaymentIntent)
            .where(PaymentIntent.status == "CREATED", PaymentIntent.created_at < now - timedelta(hours=24))
            .values(status="EXPIRED")
        )
        await session.commit()
        intents = (await session.execute(
            select(PaymentIntent.id, PaymentIntent.gateway_txn_id).where(
                PaymentIntent.status == "CREATED",
                PaymentIntent.gateway_txn_id.is_not(None),
                PaymentIntent.created_at < now - timedelta(seconds=45),
            ).limit(50)
        )).all()

    client = MrAiPayClient(config["api_key"], config["secret_key"], config["brand_key"])
    for intent_id, txn_id in intents:
        try:
            async with session_pool() as session:
                outcome = await verify_and_record(session, client, txn_id)
                new_status = {"COMPLETED": "COMPLETED", "ERROR": "FAILED"}.get(outcome.status)
                if new_status:
                    await session.execute(
                        update(PaymentIntent).where(PaymentIntent.id == intent_id).values(status=new_status)
                    )
                    await session.commit()
            if outcome.newly_credited and outcome.user_id:
                await notify_user(bot, outcome.user_id, deposit_success_text(outcome.amount))
        except MrAiPayError as exc:
            logger.info("Intent %s verify skipped: %s", intent_id, exc)
        except Exception:  # noqa: BLE001
            logger.exception("Intent %s reconcile failed", intent_id)

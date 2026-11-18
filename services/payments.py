"""
Deposits (manual + Mr Ai Pay), settings and referral bonus.

Race-safety rules used everywhere here:
  * A deposit/auto-deposit is "claimed" with a conditional UPDATE
    (… WHERE status = 'PENDING'); only the caller whose UPDATE touches 1 row
    may credit the wallet. Two admins tapping Approve together can't double-credit.
  * Wallet credit, status change and referral bonus commit in ONE transaction.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import MAX_PENDING_DEPOSITS_PER_USER, REFERRAL_MIN_DEPOSIT
from database.models import (
    AutoDeposit, Deposit, DepositStatus, Referral, Setting, TransactionType, User,
)
from services.wallet import credit
from utils.crypto import decrypt, encrypt
from utils.helpers import normalize_txn_id, to_money, utcnow

logger = logging.getLogger(__name__)

# Settings whose values are secrets -> encrypted at rest when SECRET_ENCRYPTION_KEY is set.
SECRET_SETTING_KEYS = {"mraipay_api_key", "mraipay_secret_key", "mraipay_brand_key"}


class DepositError(Exception):
    pass


class DuplicateTransactionError(DepositError):
    pass


class TooManyPendingError(DepositError):
    pass


class AlreadyProcessedError(DepositError):
    pass


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------
async def get_setting(session: AsyncSession, key: str, default: str) -> str:
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        return default
    return (decrypt(row.value) if key in SECRET_SETTING_KEYS else row.value) or default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    stored = encrypt(value) if key in SECRET_SETTING_KEYS else value
    result = await session.execute(select(Setting).where(Setting.key == key))
    row = result.scalar_one_or_none()
    if row:
        row.value = stored
    else:
        session.add(Setting(key=key, value=stored))
    await session.flush()


# ----------------------------------------------------------------------
# Referral bonus
# ----------------------------------------------------------------------
async def _maybe_pay_referral_bonus(session: AsyncSession, referred_user: User, deposit_amount: Decimal) -> None:
    """Pays once per referred user, only when the deposit reaches the minimum
    (stops the ৳1-deposit bonus farming)."""
    if referred_user.referred_by is None:
        return
    if (await get_setting(session, "referral_bonus_enabled", "true")) != "true":
        return
    try:
        min_deposit = Decimal(await get_setting(session, "referral_min_deposit", str(REFERRAL_MIN_DEPOSIT)))
    except InvalidOperation:
        min_deposit = REFERRAL_MIN_DEPOSIT
    if to_money(deposit_amount) < min_deposit:
        return
    existing = await session.execute(select(Referral.id).where(Referral.referred_id == referred_user.id))
    if existing.first() is not None:
        return
    try:
        bonus_amount = to_money(Decimal(await get_setting(session, "referral_bonus_amount", "10")))
    except InvalidOperation:
        return
    if bonus_amount <= 0:
        return
    referrer = (await session.execute(select(User).where(User.id == referred_user.referred_by))).scalar_one_or_none()
    if referrer is None or referrer.id == referred_user.id:
        return

    # Insert the Referral row FIRST: its UNIQUE(referred_id) makes a concurrent double-payment fail here.
    session.add(Referral(referrer_id=referrer.id, referred_id=referred_user.id, bonus_amount=bonus_amount))
    await session.flush()
    await credit(session, referrer, bonus_amount, TransactionType.REFERRAL_BONUS,
                 note=f"Referral bonus for {referred_user.id}")


# ----------------------------------------------------------------------
# Manual deposits
# ----------------------------------------------------------------------
_create_lock = asyncio.Lock()


async def is_duplicate_txn(session: AsyncSession, transaction_id: str) -> bool:
    txn = normalize_txn_id(transaction_id) or transaction_id.strip().upper()
    result = await session.execute(
        select(Deposit.id)
        .where(func.upper(Deposit.transaction_id) == txn, Deposit.status != DepositStatus.REJECTED)
        .limit(1)
    )
    return result.first() is not None


async def create_deposit_request(
    session: AsyncSession, user: User, method_id: int, amount, transaction_id: str, sender_number: str,
) -> Deposit:
    txn = normalize_txn_id(transaction_id) or transaction_id.strip().upper()
    async with _create_lock:
        if await is_duplicate_txn(session, txn):
            raise DuplicateTransactionError()
        pending = (await session.execute(
            select(func.count(Deposit.id)).where(Deposit.user_id == user.id, Deposit.status == DepositStatus.PENDING)
        )).scalar() or 0
        if pending >= MAX_PENDING_DEPOSITS_PER_USER:
            raise TooManyPendingError()
        deposit = Deposit(
            user_id=user.id, method_id=method_id, amount=to_money(amount),
            transaction_id=txn, sender_number=sender_number, status=DepositStatus.PENDING,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
    return deposit


async def approve_deposit(session: AsyncSession, deposit_id: int) -> Deposit:
    """Atomically PENDING -> APPROVED + credit wallet (+ referral bonus). Raises AlreadyProcessedError."""
    try:
        claim = await session.execute(
            update(Deposit)
            .where(Deposit.id == deposit_id, Deposit.status == DepositStatus.PENDING)
            .values(status=DepositStatus.APPROVED, reviewed_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            await session.rollback()
            raise AlreadyProcessedError()

        deposit = (await session.execute(
            select(Deposit).where(Deposit.id == deposit_id).execution_options(populate_existing=True)
        )).scalar_one()
        user = (await session.execute(select(User).where(User.id == deposit.user_id))).scalar_one()
        await credit(session, user, deposit.amount, TransactionType.DEPOSIT,
                     note=f"Deposit approved ({deposit.transaction_id})")
        await _maybe_pay_referral_bonus(session, user, deposit.amount)
        await session.commit()
        return deposit
    except IntegrityError as exc:
        await session.rollback()
        raise AlreadyProcessedError() from exc


async def reject_deposit(session: AsyncSession, deposit_id: int, reason: str | None = None) -> Deposit:
    claim = await session.execute(
        update(Deposit)
        .where(Deposit.id == deposit_id, Deposit.status == DepositStatus.PENDING)
        .values(status=DepositStatus.REJECTED, rejection_reason=reason, reviewed_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        await session.rollback()
        raise AlreadyProcessedError()
    await session.commit()
    return (await session.execute(
        select(Deposit).where(Deposit.id == deposit_id).execution_options(populate_existing=True)
    )).scalar_one()


# ----------------------------------------------------------------------
# MR AI PAY — automated bKash/Nagad/Rocket gateway
# ----------------------------------------------------------------------
async def get_mraipay_config(session: AsyncSession) -> dict | None:
    """Config only when enabled AND all keys are set, else None (feature off)."""
    if (await get_setting(session, "mraipay_enabled", "false")) != "true":
        return None
    api_key = await get_setting(session, "mraipay_api_key", "")
    secret_key = await get_setting(session, "mraipay_secret_key", "")
    brand_key = await get_setting(session, "mraipay_brand_key", "")
    if not (api_key and secret_key and brand_key):
        return None
    return {"api_key": api_key, "secret_key": secret_key, "brand_key": brand_key}


_auto_lock = asyncio.Lock()


async def record_auto_deposit_if_new(
    session: AsyncSession, user: User, transaction_id: str, amount, payment_method: str | None, status: str,
) -> bool:
    """
    Idempotent. Returns True only when THIS call credited the wallet.

      new txn + COMPLETED            -> insert row, credit        -> True
      new txn + PENDING/other        -> insert row only           -> False
      known txn recorded as PENDING, now COMPLETED -> credit once -> True   (the old bug:
            a PENDING first callback blocked the real credit forever)
      known txn already COMPLETED    -> nothing                   -> False
    """
    amount = to_money(amount)
    status = (status or "").upper()
    async with _auto_lock:
        try:
            row = (await session.execute(
                select(AutoDeposit).where(AutoDeposit.transaction_id == transaction_id)
            )).scalar_one_or_none()

            if row is None:
                credit_now = status == "COMPLETED" and amount > 0
                session.add(AutoDeposit(
                    user_id=user.id, transaction_id=transaction_id, amount=amount,
                    payment_method=payment_method, status="COMPLETED" if credit_now else status or "UNKNOWN",
                ))
                await session.flush()
                if credit_now:
                    await credit(session, user, amount, TransactionType.DEPOSIT,
                                 note=f"Auto deposit via Mr Ai Pay ({transaction_id})")
                    await _maybe_pay_referral_bonus(session, user, amount)
                await session.commit()
                return credit_now

            if row.status == "COMPLETED":
                return False
            if status != "COMPLETED" or amount <= 0:
                # Still not paid (PENDING / ERROR …): just remember the latest status.
                if status and status != "COMPLETED" and row.status != status:
                    row.status = status
                    await session.commit()
                return False

            claim = await session.execute(
                update(AutoDeposit)
                .where(AutoDeposit.id == row.id, AutoDeposit.status != "COMPLETED")
                .values(status="COMPLETED", amount=amount, payment_method=payment_method)
                .execution_options(synchronize_session=False)
            )
            if claim.rowcount != 1:
                await session.rollback()
                return False
            await credit(session, user, amount, TransactionType.DEPOSIT,
                         note=f"Auto deposit via Mr Ai Pay ({transaction_id})")
            await _maybe_pay_referral_bonus(session, user, amount)
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            logger.info("Auto deposit %s already recorded concurrently", transaction_id)
            return False

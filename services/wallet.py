"""
Wallet primitives.

Every balance change is ONE atomic SQL statement
    UPDATE users SET balance = balance + :amount ...
    UPDATE users SET balance = balance - :amount ... WHERE balance >= :amount
so concurrent deposits / refunds / purchases can never overwrite each other
(the old read-modify-write in Python could lose updates).
"""
import secrets
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from database.database import engine
from database.models import Transaction, TransactionType, User
from utils.helpers import to_money, utcnow


class InsufficientBalanceError(Exception):
    pass


def _insert_for_dialect():
    return sqlite_insert if engine.dialect.name == "sqlite" else pg_insert


async def get_or_create_user_ex(session: AsyncSession, tg_user) -> tuple[User, bool]:
    """Returns (user, created). Safe against two first messages arriving at once."""
    result = await session.execute(select(User).where(User.id == tg_user.id))
    user = result.scalar_one_or_none()
    if user is not None:
        if user.username != tg_user.username:
            user.username = tg_user.username
        if user.first_name != tg_user.first_name:
            user.first_name = tg_user.first_name
        return user, False

    stmt = (
        _insert_for_dialect()(User)
        .values(
            id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name,
            referral_code=secrets.token_urlsafe(6),
            balance=Decimal("0"), total_deposit=Decimal("0"), total_spent=Decimal("0"),
            referral_earnings=Decimal("0"), is_banned=False, created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    insert_result = await session.execute(stmt)
    created = insert_result.rowcount == 1
    user = (await session.execute(
        select(User).where(User.id == tg_user.id).execution_options(populate_existing=True)
    )).scalar_one()
    return user, created


async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    user, _ = await get_or_create_user_ex(session, tg_user)
    return user


async def credit(session: AsyncSession, user: User, amount, txn_type: TransactionType, note: str | None = None) -> Transaction:
    amount = to_money(amount)
    if amount <= 0:
        raise ValueError("credit amount must be positive")
    values = {"balance": User.balance + amount}
    if txn_type == TransactionType.DEPOSIT:
        values["total_deposit"] = User.total_deposit + amount
    elif txn_type == TransactionType.REFERRAL_BONUS:
        values["referral_earnings"] = User.referral_earnings + amount

    await session.execute(
        update(User).where(User.id == user.id).values(**values).execution_options(synchronize_session=False)
    )
    txn = Transaction(user_id=user.id, type=txn_type, amount=amount, note=(note or "")[:256] or None)
    session.add(txn)
    await session.flush()
    await session.refresh(user)
    return txn


async def debit(session: AsyncSession, user: User, amount, txn_type: TransactionType, note: str | None = None) -> Transaction:
    amount = to_money(amount)
    if amount <= 0:
        raise ValueError("debit amount must be positive")
    values = {"balance": User.balance - amount}
    if txn_type == TransactionType.PURCHASE:
        values["total_spent"] = User.total_spent + amount

    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.balance >= amount)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        raise InsufficientBalanceError()

    txn = Transaction(user_id=user.id, type=txn_type, amount=amount, note=(note or "")[:256] or None)
    session.add(txn)
    await session.flush()
    await session.refresh(user)
    return txn

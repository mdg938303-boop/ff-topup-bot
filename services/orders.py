"""
Top-up order logic — built so that money can never be lost or duplicated.

Order placement ("reserve, then dispatch"):
  1. In ONE transaction: debit the wallet and create the Order  -> COMMIT.
  2. Only then call the provider API (API-linked packages).
       * provider says "rejected"      -> auto-refund (nothing was created there).
       * provider answer UNKNOWN       -> keep the money taken, flag the order
         (timeout / 5xx / garbled reply)  `needs_review`, alert the admins.
         We never auto-refund or retry blindly: the provider may already have
         accepted the order, and a refund would hand out free diamonds.
       * provider accepted             -> store its order id; the background sync
                                          (services/order_sync.py) finishes it.
  This replaces the old "call API first, charge afterwards" flow, where a timeout
  after a successful API call meant diamonds delivered for free (and a retry = double).

Status changes (deliver / cancel / sync) are "claimed" with a conditional
UPDATE … WHERE status IN (…); only one actor can win, so an admin and the sync job
can never both refund the same order.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ApiProvider, Order, OrderStatus, Package, TransactionType, User
from services.api_client import (
    STATUS_COMPLETED, STATUS_FAILED, ApiClientError, ApiRejectedError, get_provider_client,
)
from services.wallet import InsufficientBalanceError, credit, debit  # noqa: F401  (re-exported)
from utils.helpers import to_money, utcnow

logger = logging.getLogger(__name__)

DUPLICATE_WINDOW_SECONDS = 10

_MSG_PROVIDER_DOWN = "প্রোভাইডার বর্তমানে নিষ্ক্রিয়। একটু পরে আবার চেষ্টা করুন।"
_MSG_PROVIDER_REJECTED = "প্রোভাইডার এই মুহূর্তে অর্ডারটি গ্রহণ করতে পারেনি।"


class PackageUnavailableError(Exception):
    pass


class PriceChangedError(Exception):
    def __init__(self, new_price: Decimal) -> None:
        super().__init__(str(new_price))
        self.new_price = new_price


class DuplicateOrderError(Exception):
    pass


class ProviderDispatchError(Exception):
    """Provider refused the order. str(exc) is safe to show to the customer;
    `.detail` is the raw provider message (admins only). `.refunded` tells whether money was charged and returned."""

    def __init__(self, user_message: str, detail: str = "", refunded: bool = False, order_code: str = "") -> None:
        super().__init__(user_message)
        self.detail = detail
        self.refunded = refunded
        self.order_code = order_code


@dataclass
class PlaceResult:
    order: Order
    uncertain: bool = False          # provider outcome unknown -> admins must check
    detail: str = ""


# ----------------------------------------------------------------------
# tiny keyed locks (in-process; this bot runs as a single instance)
# ----------------------------------------------------------------------
_locks: "weakref.WeakValueDictionary[tuple[str, int], asyncio.Lock]" = weakref.WeakValueDictionary()


def get_lock(kind: str, user_id: int) -> asyncio.Lock:
    key = (kind, user_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


# ----------------------------------------------------------------------
# claim helpers
# ----------------------------------------------------------------------
async def _load_order(session: AsyncSession, order_id: int) -> Order | None:
    return (await session.execute(
        select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def transition_order(session: AsyncSession, order_id: int, from_statuses, to_status: OrderStatus, **extra) -> bool:
    """Atomic compare-and-set on Order.status. True only for the single winner."""
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(list(from_statuses)))
        .values(status=to_status, **extra)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def mark_delivered(session: AsyncSession, order_id: int, **extra) -> Order | None:
    ok = await transition_order(
        session, order_id, (OrderStatus.PENDING_DELIVERY, OrderStatus.PROCESSING),
        OrderStatus.DELIVERED, delivered_at=utcnow(), **extra,
    )
    if not ok:
        await session.rollback()
        return None
    await session.commit()
    return await _load_order(session, order_id)


async def cancel_and_refund(session: AsyncSession, order_id: int, note_prefix: str = "Refund", **extra) -> Order | None:
    ok = await transition_order(
        session, order_id, (OrderStatus.PENDING_DELIVERY, OrderStatus.PROCESSING),
        OrderStatus.CANCELLED, **extra,
    )
    if not ok:
        await session.rollback()
        return None
    order = await _load_order(session, order_id)
    user = (await session.execute(select(User).where(User.id == order.user_id))).scalar_one()
    await credit(session, user, order.price_paid, TransactionType.REFUND, note=f"{note_prefix}: {order.order_code}")
    await session.commit()
    return order


# ----------------------------------------------------------------------
# placing an order
# ----------------------------------------------------------------------
async def place_order(
    session: AsyncSession, user: User, package_id: int, player_id: str, expected_price: Decimal | None = None,
) -> PlaceResult:
    async with get_lock("purchase", user.id):
        pkg = (await session.execute(
            select(Package).where(Package.id == package_id).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if pkg is None or not pkg.is_active:
            raise PackageUnavailableError()

        price = to_money(pkg.price)
        if expected_price is not None and to_money(expected_price) != price:
            raise PriceChangedError(price)

        # Second line of defence against double-taps (the handler already consumes its FSM state).
        cutoff = utcnow() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
        dup = await session.execute(
            select(Order.id).where(
                Order.user_id == user.id, Order.package_id == pkg.id, Order.player_id == player_id,
                Order.created_at >= cutoff, Order.status != OrderStatus.CANCELLED,
            ).limit(1)
        )
        if dup.first() is not None:
            raise DuplicateOrderError()

        is_api = bool(pkg.api_provider_id and pkg.api_service_id)
        provider = None
        if is_api:
            provider = (await session.execute(
                select(ApiProvider).where(ApiProvider.id == pkg.api_provider_id)
            )).scalar_one_or_none()
            if provider is None or not provider.is_active:
                raise ProviderDispatchError(_MSG_PROVIDER_DOWN, detail="provider inactive/missing")

        # 1) reserve: charge + create order, atomically
        await debit(session, user, price, TransactionType.PURCHASE, note=f"Top-up: {pkg.name}")
        order = Order(
            user_id=user.id, package_id=pkg.id, package_name_snapshot=pkg.name, price_paid=price,
            player_id=player_id,
            status=OrderStatus.PROCESSING if is_api else OrderStatus.PENDING_DELIVERY,
            api_provider_id=provider.id if provider else None,
            api_status="DISPATCHING" if is_api else None,
        )
        session.add(order)
        user.last_player_id = player_id
        await session.commit()

        if not is_api:
            return PlaceResult(order)

        # 2) dispatch to the provider
        return await _dispatch_to_provider(session, order, provider, pkg.api_service_id)


async def _dispatch_to_provider(session: AsyncSession, order: Order, provider: ApiProvider, service_id: str) -> PlaceResult:
    client = get_provider_client(provider)
    try:
        api_order_id = await client.add_order(service_id, order.player_id, quantity=1, idempotency_key=order.order_code)

    except ApiRejectedError as exc:
        logger.warning("Provider rejected %s: %s", order.order_code, exc)
        refunded = await cancel_and_refund(
            session, order.id, note_prefix="Auto-refund (provider rejected)",
            api_status="REJECTED", last_api_sync=utcnow(),
        )
        raise ProviderDispatchError(
            _MSG_PROVIDER_REJECTED, detail=str(exc), refunded=refunded is not None, order_code=order.order_code,
        ) from exc

    except ApiClientError as exc:
        # Unknown outcome — DO NOT refund, DO NOT retry. A human decides.
        logger.error("Provider outcome UNKNOWN for %s: %s", order.order_code, exc)
        await session.execute(
            update(Order).where(Order.id == order.id)
            .values(needs_review=True, admin_alerted=True, api_status="UNCERTAIN", last_api_sync=utcnow())
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return PlaceResult(await _load_order(session, order.id), uncertain=True, detail=str(exc))

    await session.execute(
        update(Order).where(Order.id == order.id)
        .values(api_order_id=str(api_order_id), api_status="SUBMITTED", last_api_sync=utcnow())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return PlaceResult(await _load_order(session, order.id))


# ----------------------------------------------------------------------
# admin cancel (safe for API orders)
# ----------------------------------------------------------------------
async def admin_cancel_order(session: AsyncSession, order_id: int) -> tuple[str, Order | None]:
    """
    Outcomes: refunded | delivered_instead | blocked_processing | blocked_unreachable | not_cancellable

    An API order still running at the provider can NOT simply be refunded — the
    provider may still deliver, giving the customer diamonds AND their money back.
    So we ask the provider first.
    """
    order = await _load_order(session, order_id)
    if order is None or order.status not in (OrderStatus.PENDING_DELIVERY, OrderStatus.PROCESSING):
        return "not_cancellable", order

    if order.status == OrderStatus.PROCESSING and order.api_order_id and order.api_provider_id:
        provider = (await session.execute(
            select(ApiProvider).where(ApiProvider.id == order.api_provider_id)
        )).scalar_one_or_none()
        if provider is not None:
            try:
                status = (await get_provider_client(provider).get_status(order.api_order_id))["status"]
            except ApiClientError:
                return "blocked_unreachable", order
            if status == STATUS_COMPLETED:
                delivered = await mark_delivered(session, order_id)
                return "delivered_instead", delivered or order
            if status != STATUS_FAILED:
                return "blocked_processing", order

    refunded = await cancel_and_refund(session, order_id, note_prefix="Refund")
    return ("refunded", refunded) if refunded else ("not_cancellable", order)

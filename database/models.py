"""
Database models. Empty by default — no diamond packages exist until the
Admin creates them via the Admin Panel.

All money columns are Decimal (Numeric(12,2)) — never float.
"""
from __future__ import annotations

import enum
import secrets
import string
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Integer, Numeric,
    String, Text, TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from utils.crypto import decrypt, encrypt
from utils.helpers import utcnow


class Base(DeclarativeBase):
    pass


class EncryptedText(TypeDecorator):
    """Transparently encrypts/decrypts a text column (see utils/crypto.py)."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)


def gen_order_id() -> str:
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    return f"FF-{suffix}"


class OrderStatus(str, enum.Enum):
    PENDING_DELIVERY = "PENDING_DELIVERY"   # manual fulfillment, awaiting admin
    PROCESSING = "PROCESSING"                # dispatched to provider API, awaiting completion
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class DepositStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"
    REFERRAL_BONUS = "REFERRAL_BONUS"
    ADJUSTMENT = "ADJUSTMENT"


class User(Base):
    __tablename__ = "users"
    # Safety net: even if application code has a bug, the DB refuses negative balances.
    __table_args__ = (CheckConstraint("balance >= 0", name="ck_users_balance_nonneg"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total_deposit: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    referral_earnings: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))

    referral_code: Mapped[str] = mapped_column(String(32), unique=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=True)

    # Remembered so returning customers don't have to retype their Player ID
    last_player_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    orders: Mapped[list["Order"]] = relationship(back_populates="user")


class ApiProvider(Base):
    """A diamond top-up API reseller/provider. Empty by default."""
    __tablename__ = "api_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    # "EPINBY" (REST JSON, X-API-KEY header) or "SMM_GENERIC" (form-data action=add/status/balance)
    provider_type: Mapped[str] = mapped_column(String(32), default="SMM_GENERIC")
    base_url: Mapped[str] = mapped_column(String(256))
    api_key: Mapped[str] = mapped_column(EncryptedText)
    api_secret: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    packages: Mapped[list["Package"]] = relationship(back_populates="api_provider")


class Package(Base):
    """A diamond top-up package, e.g. '100 Diamond — ৳95'. Empty by default."""
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))          # e.g. "100 Diamond"
    diamond_amount: Mapped[str] = mapped_column(String(32))  # free text: "100", "Weekly Membership" etc
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # If both are set, orders for this package are delivered AUTOMATICALLY via
    # the provider's API. If either is null, the package falls back to MANUAL
    # admin delivery.
    api_provider_id: Mapped[int | None] = mapped_column(ForeignKey("api_providers.id"), nullable=True)
    api_service_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cost_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)  # provider's rate, for margin reference

    # --- catalog import / automatic pricing (services/catalog.py) -------------------------------
    # auto_price=True: the selling price is recomputed from the provider's live cost
    #   price = src_cost × exchange-rate  +  markup   (markup_type PERCENT|FIXED)
    src_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)   # provider cost, 4 decimals
    auto_price: Mapped[bool] = mapped_column(Boolean, default=False)
    markup_type: Mapped[str | None] = mapped_column(String(8), nullable=True)         # PERCENT | FIXED
    markup_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    src_game_id: Mapped[int | None] = mapped_column(Integer, nullable=True)           # where it was imported from,
    src_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)       # used to refresh prices cheaply

    api_provider: Mapped["ApiProvider | None"] = relationship(back_populates="packages")
    orders: Mapped[list["Order"]] = relationship(back_populates="package")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_code: Mapped[str] = mapped_column(String(32), unique=True, default=gen_order_id)

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id"))

    package_name_snapshot: Mapped[str] = mapped_column(String(128))
    price_paid: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # The player-supplied Free Fire UID — NOT sensitive, stored in plaintext.
    player_id: Mapped[str] = mapped_column(String(32))

    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.PENDING_DELIVERY, index=True)
    delivery_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Populated only for API-fulfilled orders (see services/order_sync.py)
    api_provider_id: Mapped[int | None] = mapped_column(ForeignKey("api_providers.id"), nullable=True)
    api_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    api_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_api_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # True when the provider's answer was unclear (timeout / partial delivery …)
    # and a human must look at it. Money is NOT auto-refunded in that case.
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_alerted: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="orders")
    package: Mapped["Package"] = relationship(back_populates="orders")


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    method_name: Mapped[str] = mapped_column(String(64))
    account_number: Mapped[str] = mapped_column(String(64))
    account_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    deposits: Mapped[list["Deposit"]] = relationship(back_populates="method")


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    method_id: Mapped[int] = mapped_column(ForeignKey("payment_methods.id"))

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Stored normalised (upper-case, no spaces) — duplicates are rejected in services/payments.py
    transaction_id: Mapped[str] = mapped_column(String(128), index=True)
    sender_number: Mapped[str] = mapped_column(String(32))

    status: Mapped[DepositStatus] = mapped_column(Enum(DepositStatus), default=DepositStatus.PENDING, index=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship()
    method: Mapped["PaymentMethod"] = relationship(back_populates="deposits")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship()


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    referred_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    bonus_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AutoDeposit(Base):
    """One row per Mr Ai Pay transaction_id ever seen (the unique constraint is the
    idempotency guard: the same payment can never be credited twice)."""
    __tablename__ = "auto_deposits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    transaction_id: Mapped[str] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentIntent(Base):
    """A payment link we created. If the gateway tells us its transaction id up
    front, a background job re-verifies it — so a user who pays and then closes
    the browser before the redirect still gets credited."""
    __tablename__ = "payment_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    gateway_txn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="CREATED", index=True)  # CREATED / COMPLETED / FAILED / EXPIRED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(128))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

import uuid
from datetime import datetime, timezone

from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Numeric, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('manager', 'lead', 'client')"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))


class ClientOrganization(Base):
    __tablename__ = "client_organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"))


class Quote(Base):
    __tablename__ = "quotes"
    __table_args__ = (CheckConstraint("status = 'draft'"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                    default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    client_org_id: Mapped[int] = mapped_column(ForeignKey("client_organizations.id"))
    comment: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


# The original quotes columns are retained for an additive upgrade of EK1 data.
# Commercial terms and workflow state now belong to QuoteVersion.
class ClientMembership(Base):
    __tablename__ = "client_memberships"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    client_org_id: Mapped[int] = mapped_column(ForeignKey("client_organizations.id"))


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("price > 0"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class QuoteVersion(Base):
    __tablename__ = "quote_versions"
    __table_args__ = (
        UniqueConstraint("quote_id", "number"),
        CheckConstraint("status IN ('draft', 'pending', 'ready', 'returned', 'sent', 'accepted', 'declined', 'expired')"),
        CheckConstraint("discount >= 0 AND discount <= 100"),
        CheckConstraint("subtotal >= 0 AND total >= 0"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"), index=True)
    number: Mapped[int] = mapped_column()
    client_org_id: Mapped[int] = mapped_column(ForeignKey("client_organizations.id"))
    comment: Mapped[str] = mapped_column(String(1000), default="")
    discount: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class VersionItem(Base):
    __tablename__ = "version_items"
    __table_args__ = (UniqueConstraint("version_id", "product_id"), CheckConstraint("quantity > 0"))
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("quote_versions.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(100))
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3))


class LeadDecision(Base):
    __tablename__ = "lead_decisions"
    version_id: Mapped[str] = mapped_column(ForeignKey("quote_versions.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ClientDecision(Base):
    __tablename__ = "client_decisions"
    version_id: Mapped[str] = mapped_column(ForeignKey("quote_versions.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    client_org_id: Mapped[int] = mapped_column(ForeignKey("client_organizations.id"))
    action: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HistoryEvent(Base):
    __tablename__ = "history_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("quote_versions.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)

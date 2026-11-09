"""Version state changes. Every caller commits state, decisions and events together."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.models import (ClientDecision, ClientMembership, ClientOrganization,
                        HistoryEvent, LeadDecision, Product, Quote, QuoteVersion, VersionItem)

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999.99")


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def now(session):
    # PostgreSQL now() is transaction-start time: unsuitable after waiting for a lock.
    if session.bind.dialect.name == "postgresql":
        return session.scalar(select(func.clock_timestamp()))
    return datetime.now(timezone.utc)


def role(user, *allowed):
    if user.role not in allowed:
        raise HTTPException(403, "Action forbidden for this role")


def organization(session, org_id, user):
    org = session.get(ClientOrganization, org_id)
    if org is None or org.manager_id != user.id:
        raise HTTPException(404, "Client organization not found")
    return org


def lock_quote(session, quote_id, user):
    quote = session.scalar(select(Quote).where(Quote.id == str(quote_id))
                           .with_for_update().execution_options(populate_existing=True))
    if quote is None or (user.role == "manager" and quote.owner_id != user.id):
        raise HTTPException(404, "Quote not found")
    return quote


def visible(session, version, user):
    if user.role == "manager":
        quote = session.get(Quote, version.quote_id)
        org = session.get(ClientOrganization, version.client_org_id)
        return quote.owner_id == user.id and org.manager_id == user.id
    if user.role == "lead":
        decision = session.get(LeadDecision, version.id)
        return version.status == "pending" or (decision is not None and decision.user_id == user.id)
    membership = session.get(ClientMembership, user.id)
    return (membership is not None and membership.client_org_id == version.client_org_id
            and version.sent_at is not None)


def lock_version(session, quote_id, version_id, user):
    lock_quote(session, quote_id, user)
    version = session.scalar(select(QuoteVersion).where(
        QuoteVersion.id == str(version_id), QuoteVersion.quote_id == str(quote_id)
    ).with_for_update().execution_options(populate_existing=True))
    if version is None or not visible(session, version, user):
        raise HTTPException(404, "Version not found")
    return version


def event(session, version, user, action):
    session.add(HistoryEvent(version_id=version.id, actor_id=user.id if user else None,
                             action=action, status=version.status, created_at=now(session)))


def new_version(session, quote, org_id, comment, number, user):
    version = QuoteVersion(quote_id=quote.id, number=number, client_org_id=org_id,
                           comment=comment, status="draft")
    session.add(version)
    session.flush()
    event(session, version, user, "created")
    return version


def require_state(version, expected):
    if version.status != expected:
        raise HTTPException(409, "Operation is not allowed in this state")


def future(session, deadline):
    if deadline is None or utc(deadline) <= now(session):
        raise HTTPException(422, "Expiration must be in the future")


def calculate(session, items, discount):
    snapshots = []
    raw_subtotal = Decimal("0")
    # Read all catalog entries in one SQL statement for a consistent catalog snapshot.
    products = {p.id: p for p in session.scalars(select(Product).where(
        Product.id.in_([item.product_id for item in items])))}
    for item in items:
        product = products.get(item.product_id)
        if product is None:
            raise HTTPException(422, "Unknown product")
        raw_subtotal += product.price * item.quantity
        snapshots.append(dict(product_id=product.id, name=product.name,
                              price=product.price, quantity=item.quantity))
    subtotal = raw_subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
    total = (subtotal * (Decimal("100") - discount) / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
    if subtotal > MAX_AMOUNT or total > MAX_AMOUNT:
        raise HTTPException(422, "Amount exceeds storage limit")
    return snapshots, subtotal, total


def set_terms(session, version, data, user):
    require_state(version, "draft")
    organization(session, data.client_org_id, user)
    future(session, data.expires_at)
    snapshots, subtotal, total = calculate(session, data.items, data.discount)
    session.execute(delete(VersionItem).where(VersionItem.version_id == version.id))
    for snapshot in snapshots:
        session.add(VersionItem(version_id=version.id, **snapshot))
    version.client_org_id = data.client_org_id
    version.comment = data.comment
    version.discount = data.discount
    version.expires_at = data.expires_at
    version.subtotal = subtotal
    version.total = total
    event(session, version, user, "edited")


def finalize(session, version, user):
    require_state(version, "draft")
    future(session, version.expires_at)
    items = session.scalars(select(VersionItem).where(VersionItem.version_id == version.id)).all()
    if not items:
        raise HTTPException(422, "Add at least one item before finalizing")
    # Finalization freezes the current catalog prices, not a stale draft preview.
    snapshots, subtotal, total = calculate(session, items, version.discount)
    for item, snapshot in zip(items, snapshots):
        item.name, item.price = snapshot["name"], snapshot["price"]
    version.subtotal, version.total = subtotal, total
    version.status = "pending" if version.discount > Decimal("10") else "ready"
    event(session, version, user, "finalized")


def lead_decide(session, version, data, user):
    require_state(version, "pending")
    if data.action == "approve":
        future(session, version.expires_at)
    session.add(LeadDecision(version_id=version.id, user_id=user.id, action=data.action,
                             comment=data.comment, created_at=now(session)))
    version.status = "ready" if data.action == "approve" else "returned"
    event(session, version, user, "approved" if data.action == "approve" else "returned")


def send(session, version, user):
    require_state(version, "ready")
    future(session, version.expires_at)
    if version.discount > Decimal("10"):
        decision = session.get(LeadDecision, version.id)
        if decision is None or decision.action != "approve":
            raise HTTPException(409, "This version needs approval")
    version.sent_at = now(session)
    version.status = "sent"
    event(session, version, user, "sent")


def expire(session, version):
    if version.status == "sent" and utc(version.expires_at) <= now(session):
        version.status = "expired"
        event(session, version, None, "expired")
        session.flush()
        return True
    return False


def client_decide(session, version, data, user):
    if expire(session, version):
        # Expiration is a successful system transition even though the user action fails.
        session.commit()
        raise HTTPException(409, "Version has expired")
    require_state(version, "sent")
    session.add(ClientDecision(version_id=version.id, user_id=user.id,
                              client_org_id=version.client_org_id, action=data.action,
                              created_at=now(session)))
    version.status = "accepted" if data.action == "accept" else "declined"
    event(session, version, user, version.status)


def version_output(session, version, user):
    # Explicit allowlist: no ORM serialization of internal decisions to a client.
    result = dict(id=version.id, quote_id=version.quote_id, number=version.number,
                  client_org_id=version.client_org_id, comment=version.comment,
                  discount=str(version.discount), subtotal=str(version.subtotal),
                  total=str(version.total), expires_at=utc(version.expires_at) if version.expires_at else None,
                  status=version.status, created_at=utc(version.created_at),
                  sent_at=utc(version.sent_at) if version.sent_at else None,
                  items=[dict(product_id=i.product_id, name=i.name, price=str(i.price), quantity=str(i.quantity))
                         for i in session.scalars(select(VersionItem).where(
                             VersionItem.version_id == version.id).order_by(VersionItem.id))])
    client_decision = session.get(ClientDecision, version.id)
    result["client_decision"] = (dict(action=client_decision.action, user_id=client_decision.user_id,
                                     created_at=utc(client_decision.created_at)) if client_decision else None)
    if user.role != "client":
        decision = session.get(LeadDecision, version.id)
        result["lead_decision"] = (dict(action=decision.action, comment=decision.comment,
                                       user_id=decision.user_id, created_at=utc(decision.created_at))
                                   if decision else None)
    return result

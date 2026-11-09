import os
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import (Base, ClientOrganization, User, Product, ClientMembership,
                        Quote, QuoteVersion, SchemaMigration)
from app.security import password_hasher
from app.workflow import new_version


DEMO_USERS = ((1, "manager1", "manager"), (2, "manager2", "manager"),
              (3, "lead", "lead"), (4, "client", "client"),
              (5, "client2", "client"), (6, "client_peer", "client"))


def initialize(engine, demo_password):
    if len(demo_password) < 12 or demo_password.startswith("replace-with-"):
        raise ValueError("Set DEMO_PASSWORD to a local password of at least 12 characters")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        for user_id, username, role in DEMO_USERS:
            if session.scalar(select(User).where(User.username == username)) is None:
                session.add(User(id=user_id, username=username, role=role,
                                 password_hash=password_hasher.hash(demo_password)))
        session.flush()
        for org_id, name, manager_id in ((1, "Demo Client A", 1), (2, "Demo Client B", 2)):
            if session.get(ClientOrganization, org_id) is None:
                session.add(ClientOrganization(id=org_id, name=name, manager_id=manager_id))
        session.flush()
        for user_id, org_id in ((4, 1), (5, 2), (6, 1)):
            if session.get(ClientMembership, user_id) is None:
                session.add(ClientMembership(user_id=user_id, client_org_id=org_id))
        for product_id, name, price in ((1, "Service A", "100.00"), (2, "Service B", "250.00")):
            if session.get(Product, product_id) is None:
                session.add(Product(id=product_id, name=name, price=Decimal(price)))
        # Additive migration: keep original users, password hashes and quote IDs.
        # All new tables are created above; the data migration is one transaction.
        if session.get(SchemaMigration, "001_versions") is None:
            quotes = session.scalars(select(Quote).where(~select(QuoteVersion.id).where(
                QuoteVersion.quote_id == Quote.id).exists())).all()
            for quote in quotes:
                new_version(session, quote, quote.client_org_id, quote.comment, 1,
                            session.get(User, quote.owner_id))
            session.add(SchemaMigration(name="001_versions"))


if __name__ == "__main__":
    db = make_engine()
    try:
        initialize(db, os.environ["DEMO_PASSWORD"])
        print("Database initialized; existing users and drafts preserved.")
    finally:
        db.dispose()

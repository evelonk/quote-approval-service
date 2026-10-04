import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.models import Base, ClientOrganization, User
from app.security import password_hasher


DEMO_USERS = ((1, "manager1", "manager"), (2, "manager2", "manager"),
              (3, "lead", "lead"), (4, "client", "client"))


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


if __name__ == "__main__":
    db = make_engine()
    try:
        initialize(db, os.environ["DEMO_PASSWORD"])
        print("Database initialized; existing users and drafts preserved.")
    finally:
        db.dispose()

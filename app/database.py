import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL


def make_engine(database_url=None):
    if database_url is None:
        database_url = URL.create(
            "postgresql+psycopg",
            username=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            host=os.environ["DB_HOST"],
            database=os.environ["DB_NAME"],
        )
    engine = create_engine(database_url, pool_pre_ping=True)
    # SQLite is used only by isolated automated tests.
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine

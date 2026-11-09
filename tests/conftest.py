import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.database import make_engine
from app.init_db import initialize
from app.main import create_app

PASSWORD = "core-test-password-123"


@pytest.fixture
def core_db(tmp_path):
    base_url = os.environ.get("CORE_TEST_DATABASE_URL")
    admin = None
    if base_url:
        if not base_url.startswith("postgresql"):
            raise ValueError("CORE_TEST_DATABASE_URL must name a dedicated PostgreSQL test database")
        admin = create_engine(base_url)
        schema = "test_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(base_url).update_query_dict({"options": "-csearch_path=" + schema})
    else:
        url = "sqlite:///" + str(tmp_path / "core.db")
    engine = make_engine(url)
    try:
        initialize(engine, PASSWORD)
        yield engine, url
    finally:
        engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


@pytest.fixture
def api(core_db):
    with TestClient(create_app(core_db[1])) as client:
        yield client

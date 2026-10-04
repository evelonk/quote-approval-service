import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.init_db import initialize
from app.main import create_app
from app.models import Quote

PASSWORD = "only-for-tests-123"


@pytest.fixture
def database_url(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    engine = make_engine(url)
    initialize(engine, PASSWORD)
    engine.dispose()
    return url


@pytest.fixture
def client(database_url):
    with TestClient(create_app(database_url)) as client:
        yield client


def auth(username="manager1"):
    return (username, PASSWORD)


def test_create_read_and_restart(database_url):
    with TestClient(create_app(database_url)) as client:
        response = client.post("/quotes", json={"client_org_id": 1, "comment": "Draft"}, auth=auth())
        assert response.status_code == 201
        quote = response.json()
        assert quote["status"] == "draft"
        assert quote["owner_id"] == 1
    # Re-initialization and a fresh application must not erase saved drafts.
    engine = make_engine(database_url)
    initialize(engine, "different-seed-password")
    engine.dispose()
    with TestClient(create_app(database_url)) as client:
        response = client.get("/quotes/" + quote["id"], auth=auth())
        assert response.status_code == 200
        assert response.json() == quote


@pytest.mark.parametrize("credentials", [None, ("manager1", "wrong"), ("unknown", PASSWORD)])
def test_authentication(client, credentials):
    response = client.post("/quotes", json={"client_org_id": 1}, auth=credentials)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


@pytest.mark.parametrize("username", ["lead", "client"])
def test_other_roles_cannot_create_or_read_drafts(client, username):
    draft = client.post("/quotes", json={"client_org_id": 1}, auth=auth()).json()
    assert client.post("/quotes", json={"client_org_id": 1}, auth=auth(username)).status_code == 403
    response = client.get("/quotes/" + draft["id"], auth=auth(username))
    assert response.status_code == 403
    assert "comment" not in response.json()


def test_other_manager_cannot_read_draft(client):
    draft = client.post("/quotes", json={"client_org_id": 1, "comment": "private"}, auth=auth()).json()
    response = client.get("/quotes/" + draft["id"], auth=auth("manager2"))
    assert response.status_code == 404
    assert "private" not in response.text
    assert client.get("/quotes/" + draft["id"], auth=auth()).json() == draft


@pytest.mark.parametrize("org_id", [2, 999])
def test_unassigned_or_missing_organization(client, org_id):
    assert client.post("/quotes", json={"client_org_id": org_id}, auth=auth()).status_code == 404


@pytest.mark.parametrize("extra", [
    {"owner_id": 2}, {"role": "lead"}, {"status": "approved"}, {"total": 1},
    {"comment": "x" * 1001}, {"client_org_id": 0}, {"client_org_id": "1"},
])
def test_invalid_or_server_owned_fields_do_not_create_draft(client, database_url, extra):
    response = client.post("/quotes", json={"client_org_id": 1, **extra}, auth=auth())
    assert response.status_code == 422
    engine = make_engine(database_url)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(Quote)) == 0
    engine.dispose()


def test_health_and_missing_draft(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/quotes/00000000-0000-0000-0000-000000000000", auth=auth()).status_code == 404

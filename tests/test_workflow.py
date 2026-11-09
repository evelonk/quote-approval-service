from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import workflow as w
from app.init_db import initialize
from app.models import (Base, ClientDecision, HistoryEvent, LeadDecision, Product, Quote,
                        QuoteVersion, SchemaMigration, User, ClientOrganization)
from app.database import make_engine

PASSWORD = "core-test-password-123"


def auth(name="manager1"):
    return (name, PASSWORD)


def terms(discount="10", org=1):
    return dict(client_org_id=org, comment="Client comment", discount=discount,
                expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                items=[dict(product_id=1, quantity="2")])


def draft(api, manager="manager1", org=1):
    response = api.post("/quotes", auth=auth(manager), json={"client_org_id": org})
    assert response.status_code == 201, response.text
    qid = response.json()["id"]
    versions = api.get(f"/quotes/{qid}/versions", auth=auth(manager)).json()
    return qid, f"/quotes/{qid}/versions/{versions[0]['id']}"


def prepared(api, discount="10"):
    qid, path = draft(api)
    assert api.put(path, auth=auth(), json=terms(discount)).status_code == 200
    assert api.post(path + "/finalize", auth=auth()).status_code == 200
    return qid, path


def sent(api, discount="10"):
    qid, path = prepared(api, discount)
    if Decimal(discount) > 10:
        assert api.post(path + "/approval", auth=auth("lead"),
                        json={"action": "approve", "comment": "SECRET INTERNAL"}).status_code == 200
    assert api.post(path + "/send", auth=auth()).status_code == 200
    return qid, path


def history(api, path, name="manager1"):
    r = api.get(path + "/history", auth=auth(name))
    assert r.status_code == 200
    return r.json()


def test_standard_scenario(api):
    _, path = prepared(api)
    version = api.get(path, auth=auth()).json()
    assert Decimal(version["subtotal"]) == 200
    assert Decimal(version["total"]) == 180
    assert version["status"] == "ready"
    assert api.get(path, auth=auth("client")).status_code == 404
    assert api.post(path + "/send", auth=auth()).status_code == 200
    response = api.post(path + "/decision", auth=auth("client"), json={"action": "accept"})
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert "lead_decision" not in response.json()
    assert [e["action"] for e in history(api, path)] == ["created", "edited", "finalized", "sent", "accepted"]


def test_approval_new_version_and_old_snapshot(api, core_db):
    qid, path = prepared(api, "15")
    assert api.post(path + "/send", auth=auth()).status_code == 409
    assert api.post(path + "/approval", auth=auth(), json={"action": "approve"}).status_code == 403
    assert api.post(path + "/approval", auth=auth("lead"),
                    json={"action": "approve", "comment": "SECRET INTERNAL"}).status_code == 200
    assert api.post(path + "/send", auth=auth()).status_code == 200
    old = api.get(path, auth=auth("client")).json()
    assert Decimal(old["total"]) == 170
    assert api.post(path + "/decision", auth=auth("client"), json={"action": "decline"}).status_code == 200
    with Session(core_db[0]) as s, s.begin():
        s.get(Product, 1).price = Decimal("300")
    old_declined = api.get(path, auth=auth()).json()
    assert Decimal(old_declined["items"][0]["price"]) == 100
    assert Decimal(old_declined["total"]) == 170
    response = api.post(f"/quotes/{qid}/versions", auth=auth())
    assert response.status_code == 201
    new = response.json()
    assert new["number"] == 2 and new["id"] != old["id"] and new["lead_decision"] is None
    new_path = f"/quotes/{qid}/versions/{new['id']}"
    assert api.put(new_path, auth=auth(), json=terms("15")).status_code == 200
    assert api.post(new_path + "/finalize", auth=auth()).json()["status"] == "pending"
    assert api.post(new_path + "/send", auth=auth()).status_code == 409
    assert api.put(path, auth=auth(), json=terms()).status_code == 409
    assert api.get(path, auth=auth()).json() == old_declined


def test_lead_queue_return_and_history(api):
    qid, path = draft(api)
    assert api.get(path, auth=auth("lead")).status_code == 404
    assert api.get("/versions", auth=auth("lead")).json() == []
    api.put(path, auth=auth(), json=terms("15"))
    api.post(path + "/finalize", auth=auth())
    assert len(api.get("/versions", auth=auth("lead")).json()) == 1
    r = api.post(path + "/approval", auth=auth("lead"), json={"action": "return", "comment": "Review"})
    assert r.json()["status"] == "returned"
    assert history(api, path, "lead")[-1]["action"] == "returned"
    assert api.post(f"/quotes/{qid}/versions", auth=auth()).status_code == 201
    assert api.post(f"/quotes/{qid}/versions", auth=auth()).status_code == 409


def test_client_fields_and_permissions(api):
    qid, path = sent(api, "15")
    for route in (path, f"/quotes/{qid}/versions", "/versions", path + "/history"):
        r = api.get(route, auth=auth("client"))
        assert r.status_code == 200
        assert "SECRET INTERNAL" not in r.text
        assert "lead_decision" not in r.text
        assert '"approved"' not in r.text
    assert api.get(path, auth=auth("client2")).status_code == 404
    assert api.post(path + "/decision", auth=auth("client2"), json={"action": "accept"}).status_code == 404
    assert api.post(path + "/decision", auth=auth(), json={"action": "accept"}).status_code == 403
    assert api.get("/catalog", auth=auth("client")).status_code == 403
    assert api.get("/catalog").status_code == 401
    assert api.get("/catalog", auth=auth()).status_code == 200


def test_mismatched_quote_and_version(api):
    qid, path = prepared(api)
    qid2, _ = draft(api)
    wrong = path.replace(qid, qid2)
    for suffix in ("", "/history"):
        assert api.get(wrong + suffix, auth=auth()).status_code == 404
    assert api.post(wrong + "/send", auth=auth()).status_code == 404
    assert api.get(path, auth=auth("manager2")).status_code == 404
    assert api.get(path, auth=auth()).json()["status"] == "ready"


@pytest.mark.parametrize("change", [
    {"discount": "-1"}, {"discount": "100.01"}, {"discount": "1.001"},
    {"discount": "NaN"}, {"total": "1"}, {"owner_id": 2}, {"status": "ready"},
    {"expires_at": "2020-01-01T00:00:00Z"}, {"expires_at": "2099-01-01T00:00:00"},
    {"items": []}, {"items": [{"product_id": 999, "quantity": "1"}]},
    {"items": [{"product_id": 1, "quantity": "0"}]},
    {"items": [{"product_id": 1, "quantity": "0.0001"}]},
    {"items": [{"product_id": 1, "quantity": "1", "price": "1"}]},
    {"items": [{"product_id": 1, "quantity": "1"}] * 2},
    {"client_org_id": 2},
])
def test_invalid_terms_do_not_change_version(api, change):
    _, path = draft(api)
    before = api.get(path, auth=auth()).json()
    events = history(api, path)
    response = api.put(path, auth=auth(), json={**terms(), **change})
    assert response.status_code in (404, 422), response.text
    assert api.get(path, auth=auth()).json() == before
    assert history(api, path) == events


def test_money_rounding_overflow_and_finalize_snapshot(api, core_db):
    _, path = draft(api)
    with Session(core_db[0]) as s, s.begin():
        s.get(Product, 1).price = Decimal("0.05")
    data = terms("10")
    data["items"][0]["quantity"] = "1"
    r = api.put(path, auth=auth(), json=data).json()
    assert Decimal(r["total"]) == Decimal("0.05")  # 0.045 -> 0.05 HALF_UP
    with Session(core_db[0]) as s, s.begin():
        s.get(Product, 1).price = Decimal("999999999999.99")
    data["items"][0]["quantity"] = "2"
    before = api.get(path, auth=auth()).json()
    assert api.put(path, auth=auth(), json=data).status_code == 422
    assert api.get(path, auth=auth()).json() == before
    with Session(core_db[0]) as s, s.begin():
        s.get(Product, 1).price = Decimal("200")
    r = api.post(path + "/finalize", auth=auth()).json()
    assert Decimal(r["items"][0]["price"]) == 200
    assert Decimal(r["total"]) == 180


def test_expiration_is_saved_once_even_on_rejected_decision(api, core_db):
    _, path = sent(api)
    vid = path.rsplit("/", 1)[1]
    with Session(core_db[0]) as s, s.begin():
        s.get(QuoteVersion, vid).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert api.post(path + "/decision", auth=auth("client"), json={"action": "accept"}).status_code == 409
    for _ in range(2):
        assert api.get(path, auth=auth("client")).json()["status"] == "expired"
        assert api.post(path + "/decision", auth=auth("client"), json={"action": "decline"}).status_code == 409
    assert [e["action"] for e in history(api, path)].count("expired") == 1
    with Session(core_db[0]) as s:
        assert s.get(ClientDecision, vid) is None


def test_accepted_does_not_expire_and_cannot_be_overwritten(api, core_db):
    _, path = sent(api)
    assert api.post(path + "/decision", auth=auth("client"), json={"action": "accept"}).status_code == 200
    with Session(core_db[0]) as s, s.begin():
        s.get(QuoteVersion, path.rsplit("/", 1)[1]).expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    before = history(api, path)
    assert api.get(path, auth=auth("client")).json()["status"] == "accepted"
    assert api.post(path + "/decision", auth=auth("client_peer"), json={"action": "decline"}).status_code == 409
    assert history(api, path) == before
    assert api.delete(path + "/history", auth=auth()).status_code == 405


@pytest.mark.parametrize("operation", ["edit", "finalize", "approval", "send", "decision", "next"])
def test_failure_between_state_and_event_rolls_back(api, core_db, monkeypatch, operation):
    if operation == "edit":
        _, path = draft(api)
    elif operation == "finalize":
        _, path = draft(api)
        api.put(path, auth=auth(), json=terms())
    elif operation == "approval":
        _, path = prepared(api, "15")
    elif operation == "send":
        _, path = prepared(api)
    else:
        _, path = sent(api)
        if operation == "next":
            api.post(path + "/decision", auth=auth("client"), json={"action": "decline"})
    before = api.get(path, auth=auth()).json()
    events = history(api, path)
    def broken(*args, **kwargs):
        raise RuntimeError("Injected event write failure")
    with monkeypatch.context() as m:
        m.setattr(w, "event", broken)
        with pytest.raises(RuntimeError, match="Injected"):
            if operation == "edit":
                api.put(path, auth=auth(), json=terms())
            elif operation == "approval":
                api.post(path + "/approval", auth=auth("lead"), json={"action": "approve"})
            elif operation == "decision":
                api.post(path + "/decision", auth=auth("client"), json={"action": "accept"})
            elif operation == "next":
                api.post(path.rsplit("/", 1)[0], auth=auth())
            else:
                api.post(path + "/" + operation, auth=auth())
    assert api.get(path, auth=auth()).json() == before
    assert history(api, path) == events
    with Session(core_db[0]) as s:
        assert s.scalar(select(func.count()).select_from(QuoteVersion)) == 1


def test_upgrade_preserves_legacy_quotes_and_passwords(core_db):
    # core_db is a fresh private database/schema for this test only.
    engine = core_db[0]
    Base.metadata.drop_all(engine)
    try:
        Base.metadata.create_all(engine, tables=[User.__table__, ClientOrganization.__table__, Quote.__table__])
        with Session(engine) as s, s.begin():
            s.add(User(id=1, username="manager1", role="manager", password_hash="preserved-hash"))
            s.flush()
            s.add(ClientOrganization(id=1, name="Existing", manager_id=1))
            s.flush()
            s.add(Quote(id="legacy-id", owner_id=1, client_org_id=1, comment="Saved draft"))
        initialize(engine, PASSWORD)
        initialize(engine, "another-test-password")
        with Session(engine) as s:
            assert s.get(User, 1).password_hash == "preserved-hash"
            assert s.get(Quote, "legacy-id").comment == "Saved draft"
            v = s.scalar(select(QuoteVersion).where(QuoteVersion.quote_id == "legacy-id"))
            assert v.comment == "Saved draft" and v.status == "draft"
            assert s.scalar(select(func.count()).select_from(QuoteVersion)) == 1
            assert s.scalar(select(func.count()).select_from(HistoryEvent)) == 1
            assert s.get(SchemaMigration, "001_versions") is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize("discount", ["10", "15"])
def test_expiration_before_send_allows_new_draft_without_rewriting_old(api, core_db, discount):
    qid, path = prepared(api, discount)
    vid = path.rsplit("/", 1)[1]
    with Session(core_db[0]) as s, s.begin():
        s.get(QuoteVersion, vid).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    before = api.get(path, auth=auth()).json()
    events = history(api, path)
    response = api.post(f"/quotes/{qid}/versions", auth=auth())
    assert response.status_code == 201 and response.json()["number"] == 2
    if discount == "15":
        assert api.post(path + "/approval", auth=auth("lead"), json={"action": "approve"}).status_code == 422
    else:
        assert api.post(path + "/send", auth=auth()).status_code == 422
    assert api.get(path, auth=auth()).json() == before
    assert history(api, path) == events


def test_repeated_finalization_and_approval_are_rejected(api):
    _, path = prepared(api, "15")
    events = history(api, path)
    assert api.post(path + "/finalize", auth=auth()).status_code == 409
    assert history(api, path) == events
    assert api.post(path + "/approval", auth=auth("lead"), json={"action": "approve"}).status_code == 200
    events = history(api, path)
    assert api.post(path + "/approval", auth=auth("lead"), json={"action": "return"}).status_code == 409
    assert history(api, path) == events

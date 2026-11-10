"""Independent boundary checks for SR-02–SR-08, run on the core_db backend."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import workflow as w
from app.models import ClientDecision, ClientOrganization, Product, QuoteVersion
from test_workflow import auth, draft, history, prepared, sent, terms


@pytest.mark.parametrize("discount,total,state", [
    ("0", "200.00", "ready"), ("10", "180.00", "ready"),
    ("10.01", "179.98", "pending"), ("100", "0.00", "pending"),
])
def test_discount_boundaries(api, discount, total, state):
    _, path = draft(api)
    response = api.put(path, auth=auth(), json=terms(discount))
    assert response.status_code == 200
    assert Decimal(response.json()["total"]) == Decimal(total)
    response = api.post(path + "/finalize", auth=auth())
    assert response.status_code == 200 and response.json()["status"] == state
    if state == "pending":
        assert api.post(path + "/send", auth=auth()).status_code == 409


def test_round_sum_before_discount_without_rounding_each_line(api, core_db):
    _, path = draft(api)
    with Session(core_db[0]) as session, session.begin():
        for product_id in (1, 2):
            session.get(Product, product_id).price = Decimal("0.05")
    data = terms("50")
    data["items"] = [dict(product_id=i, quantity="0.100") for i in (1, 2)]
    # Two lines of .005: sum .010 -> .01; 50% -> .005 -> .01.
    # Rounding each line first would incorrectly produce subtotal .02.
    assert api.put(path, auth=auth(), json=data).status_code == 200
    response = api.post(path + "/finalize", auth=auth())
    assert response.status_code == 200
    assert Decimal(response.json()["subtotal"]) == Decimal("0.01")
    assert Decimal(response.json()["total"]) == Decimal("0.01")


@pytest.mark.parametrize("operation", ["read", "accept", "decline"])
def test_exact_deadline_expires_once(api, core_db, monkeypatch, operation):
    _, path = sent(api)
    vid = path.rsplit("/", 1)[1]
    with Session(core_db[0]) as session:
        deadline = w.utc(session.get(QuoteVersion, vid).expires_at)
    # Fix server time to exactly the stored deadline, without timing sleeps.
    monkeypatch.setattr(w, "now", lambda session: deadline)
    if operation == "read":
        response = api.get(path, auth=auth("client"))
        assert response.status_code == 200 and response.json()["status"] == "expired"
    else:
        response = api.post(path + "/decision", auth=auth("client"), json={"action": operation})
        assert response.status_code == 409
    assert api.get(path, auth=auth("client")).json()["status"] == "expired"
    events = history(api, path)
    expired = [e for e in events if e["action"] == "expired"]
    assert len(expired) == 1 and expired[0]["actor_id"] is None
    with Session(core_db[0]) as session:
        assert session.get(ClientDecision, vid) is None


@pytest.mark.parametrize("state", ["ready", "pending", "returned", "sent", "accepted", "declined", "expired"])
def test_all_fixed_states_keep_recipient_terms_and_history(api, core_db, state):
    if state in ("pending", "returned"):
        _, path = prepared(api, "15")
        if state == "returned":
            assert api.post(path + "/approval", auth=auth("lead"), json={"action": "return"}).status_code == 200
    elif state == "ready":
        _, path = prepared(api)
    else:
        _, path = sent(api)
        if state in ("accepted", "declined"):
            action = "accept" if state == "accepted" else "decline"
            assert api.post(path + "/decision", auth=auth("client"), json={"action": action}).status_code == 200
        if state == "expired":
            with Session(core_db[0]) as session, session.begin():
                session.get(QuoteVersion, path.rsplit("/", 1)[1]).expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    # Use another permitted organization: rejection must follow from immutability,
    # rather than merely the manager not being assigned to the new recipient.
    with Session(core_db[0]) as session, session.begin():
        session.add(ClientOrganization(id=3, name="Another assigned client", manager_id=1))
    before = api.get(path, auth=auth()).json()
    assert before["status"] == state
    events = history(api, path)
    assert api.put(path, auth=auth(), json=terms("20", org=3)).status_code == 409
    assert api.get(path, auth=auth()).json() == before
    assert history(api, path) == events


def test_client_cannot_modify_own_sent_version_and_foreign_lists_do_not_leak(api):
    qid, path = sent(api, "15")
    before = api.get(path, auth=auth()).json()
    events = history(api, path)
    assert api.put(path, auth=auth("client"), json=terms()).status_code == 403
    for suffix in ("/finalize", "/send", "/approval"):
        body = {"action": "approve"} if suffix == "/approval" else {}
        assert api.post(path + suffix, auth=auth("client"), json=body).status_code == 403
    assert api.post(f"/quotes/{qid}/versions", auth=auth("client")).status_code == 403
    assert api.get(f"/quotes/{qid}/versions", auth=auth("client2")).status_code == 404
    assert api.get(path + "/history", auth=auth("client2")).status_code == 404
    assert api.get("/versions", auth=auth("client2")).json() == []
    assert api.get(path, auth=auth()).json() == before
    assert history(api, path) == events

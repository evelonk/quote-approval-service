"""These tests require real PostgreSQL; SQLite does not provide FOR UPDATE locks."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import ClientDecision, HistoryEvent, QuoteVersion
from test_workflow import auth, sent

pytestmark = pytest.mark.skipif(not os.environ.get("CORE_TEST_DATABASE_URL"),
                               reason="Requires isolated PostgreSQL via CORE_TEST_DATABASE_URL")


def test_two_client_decisions_only_one_succeeds(api, core_db):
    _, path = sent(api)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.post, path + "/decision", auth=auth(user), json={"action": action})
                   for user, action in (("client", "accept"), ("client_peer", "decline"))]
        assert sorted(f.result().status_code for f in futures) == [200, 409]
    vid = path.rsplit("/", 1)[1]
    with Session(core_db[0]) as s:
        assert s.scalar(select(func.count()).select_from(ClientDecision)) == 1
        assert s.scalar(select(func.count()).select_from(HistoryEvent).where(
            HistoryEvent.version_id == vid, HistoryEvent.action.in_(["accepted", "declined"]))) == 1
        decision = s.get(ClientDecision, vid)
        assert s.get(QuoteVersion, vid).status == ("accepted" if decision.action == "accept" else "declined")


def test_two_expiration_reads_only_one_event(api, core_db):
    _, path = sent(api)
    vid = path.rsplit("/", 1)[1]
    with Session(core_db[0]) as s, s.begin():
        s.get(QuoteVersion, vid).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.get, path, auth=auth(user)) for user in ("client", "client_peer")]
        results = [f.result() for f in futures]
    assert all(r.status_code == 200 and r.json()["status"] == "expired" for r in results)
    with Session(core_db[0]) as s:
        assert s.scalar(select(func.count()).select_from(HistoryEvent).where(
            HistoryEvent.version_id == vid, HistoryEvent.action == "expired")) == 1
        assert s.get(ClientDecision, vid) is None


def test_two_new_versions_only_one_draft(api, core_db):
    qid, path = sent(api)
    api.post(path + "/decision", auth=auth("client"), json={"action": "decline"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(api.post, f"/quotes/{qid}/versions", auth=auth()) for _ in range(2)]
        assert sorted(f.result().status_code for f in futures) == [201, 409]
    with Session(core_db[0]) as s:
        assert s.scalar(select(func.count()).select_from(QuoteVersion).where(
            QuoteVersion.quote_id == qid)) == 2


def test_deadline_rechecked_after_waiting_for_lock(api, core_db):
    _, path = sent(api)
    vid = path.rsplit("/", 1)[1]
    deadline = datetime.now(timezone.utc) + timedelta(seconds=3)
    with Session(core_db[0]) as s, s.begin():
        s.get(QuoteVersion, vid).expires_at = deadline
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(core_db[0]) as holder, holder.begin():
            holder.scalar(select(QuoteVersion).where(QuoteVersion.id == vid).with_for_update())
            result = pool.submit(api.post, path + "/decision", auth=auth("client"), json={"action": "accept"})
            limit = time.monotonic() + 2
            waiting = False
            while time.monotonic() < limit:
                with core_db[0].connect() as observer:
                    waiting = observer.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE '%quote_versions%' AND pid <> pg_backend_pid())"))
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "Request did not reach the row lock before the deadline"
            assert datetime.now(timezone.utc) < deadline
            while datetime.now(timezone.utc) <= deadline:
                time.sleep(0.02)
        response = result.result(timeout=5)
    assert response.status_code == 409
    with Session(core_db[0]) as s:
        assert s.get(QuoteVersion, vid).status == "expired"
        assert s.get(ClientDecision, vid) is None

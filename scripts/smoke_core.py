"""HTTP smoke check on a disposable/demo environment. Creates two proposals."""
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ.get("QAS_BASE_URL", "http://127.0.0.1:8000")
PASSWORD = os.environ["DEMO_PASSWORD"]


def call(method, path, user="manager1", payload=None, expected=200):
    headers = {"Authorization": "Basic " + base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()}
    body = json.dumps(payload).encode() if payload is not None else None
    if body:
        headers["Content-Type"] = "application/json"
    request = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    data = json.loads(response.read())
    assert response.status == expected, (method, path, response.status, data)
    print(method, path, response.status)
    return data


call("GET", "/health")
for discount, action in (("10", "accept"), ("15", "decline")):
    quote = call("POST", "/quotes", payload={"client_org_id": 1}, expected=201)
    qid = quote["id"]
    version = call("GET", f"/quotes/{qid}/versions")[0]
    path = f"/quotes/{qid}/versions/{version['id']}"
    data = dict(client_org_id=1, comment="HTTP smoke", items=[dict(product_id=1, quantity="2")],
                discount=discount, expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    updated = call("PUT", path, payload=data)
    assert updated["total"] == ("180.00" if discount == "10" else "170.00")
    call("POST", path + "/finalize")
    if discount == "15":
        call("POST", path + "/send", expected=409)
        call("POST", path + "/approval", user="lead", payload={"action": "approve", "comment": "INTERNAL"})
    call("POST", path + "/send")
    call("GET", path, user="manager2", expected=404)
    call("GET", path, user="client2", expected=404)
    client_data = call("GET", path, user="client")
    assert "lead_decision" not in client_data and "INTERNAL" not in json.dumps(client_data)
    call("POST", path + "/decision", user="client", payload={"action": action})
    call("POST", path + "/decision", user="client_peer", payload={"action": "accept"}, expected=409)
    call("GET", path + "/history")
    if action == "decline":
        new = call("POST", f"/quotes/{qid}/versions", expected=201)
        assert new["number"] == 2 and new["id"] != version["id"]
print("Both HTTP scenarios passed; duplicate decisions and foreign access rejected.")

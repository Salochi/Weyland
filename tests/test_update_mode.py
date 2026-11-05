from datetime import datetime, timezone

from fastapi.testclient import TestClient

from weyland.crypto import TokenBox
from weyland.db import DB
from weyland.report import item_health
from weyland.web import create_app

from test_weyland import TOKEN, env  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)


def test_health_states():
    assert item_health({}, NOW) == {"state": "ok", "reason": ""}
    # synced non-OAuth Item: Plaid returns consent_expiration_time = null
    assert item_health({"consent_expiration_time": None}, NOW)["reason"] == "no consent expiry (credential login)"
    assert item_health({"consent_expiration_time": "2027-10-01T00:00:00Z"}, NOW)["state"] == "ok"
    assert item_health({"consent_expiration_time": "2026-11-05T00:00:00Z"}, NOW)["state"] == "expiring"
    assert item_health({"consent_expiration_time": "2026-10-01T00:00:00Z"}, NOW)["state"] == "reconnect"
    assert item_health({"item_error": "ITEM_LOGIN_REQUIRED"}, NOW)["state"] == "reconnect"
    assert item_health({"item_error": "INSTITUTION_DOWN"}, NOW)["state"] == "error"


def test_update_mode_uses_access_token_and_no_slot(env):  # noqa: F811
    s, plaid, calls = env
    db, box = DB(s.db_path), TokenBox(s.key_path)
    db.add_item("item1", box.seal(TOKEN), "ins_1", "Test Bank", "bank")
    c = TestClient(create_app(s, plaid))
    h = {"tailscale-user-login": "user@example.com", "x-weyland": "1"}

    r = c.post("/api/items/item1/update-token", json={}, headers=h)
    assert r.status_code == 200, r.text
    sent = [b for p, b in calls if p == "/link/token/create"][-1]
    assert sent["access_token"] == TOKEN          # update mode, not a new Item
    assert "products" not in sent                 # products must be omitted in update mode
    assert "update" not in sent
    assert TOKEN not in r.text                    # token never reaches the browser

    c.post("/api/items/item1/update-token", json={"accounts": True}, headers=h)
    sent = [b for p, b in calls if p == "/link/token/create"][-1]
    assert sent["update"] == {"account_selection_enabled": True}

    r = c.post("/api/items/item1/reconnected", json={}, headers=h)
    assert r.status_code == 200
    assert not any(p == "/item/public_token/exchange" for p, _ in calls)
    assert db.items_ever_created() == 1

    assert c.post("/api/items/nope/update-token", json={}, headers=h).status_code == 404
    assert c.post("/api/items/item1/update-token", json={},
                  headers={"tailscale-user-login": "user@example.com"}).status_code == 400

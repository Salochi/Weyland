import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from weyland.config import Settings
from weyland.crypto import TokenBox, create_key
from weyland.db import DB
from weyland.plaid_client import PlaidClient
from weyland.sync import Syncer
from weyland import report
from weyland.web import create_app

SECRET = "s3cr3t-never-leaks"
TOKEN = "access-sandbox-abc123"


def fake_plaid(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        assert body["client_id"] == "cid" and body["secret"] == SECRET
        p = request.url.path
        acct_chk = {"account_id": "a1", "name": "Checking", "mask": "0000", "type": "depository",
                    "subtype": "checking", "balances": {"current": 100.0, "available": 90.0, "iso_currency_code": "USD"}}
        acct_cc = {"account_id": "a2", "name": "Card", "mask": "1111", "type": "credit",
                   "subtype": "credit card", "balances": {"current": 50.0, "limit": 1000.0, "iso_currency_code": "USD"}}
        if p == "/link/token/create":
            return httpx.Response(200, json={"link_token": "link-sandbox-x", "expiration": "z"})
        if p == "/item/public_token/exchange":
            return httpx.Response(200, json={"item_id": "item1", "access_token": TOKEN})
        if p == "/accounts/get":
            return httpx.Response(200, json={"accounts": [acct_chk, acct_cc]})
        if p == "/transactions/sync":
            if "cursor" not in body:
                return httpx.Response(200, json={
                    "added": [{"transaction_id": "t1", "account_id": "a1", "date": "2026-09-01", "amount": 12.5,
                               "name": "COFFEE", "pending": False,
                               "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"}}],
                    "modified": [], "removed": [], "next_cursor": "c1", "has_more": True})
            if body["cursor"] == "c1":
                return httpx.Response(200, json={
                    "added": [{"transaction_id": "t2", "account_id": "a2", "date": "2026-10-01", "amount": -2000,
                               "name": "PAYROLL", "pending": True}],
                    "modified": [], "removed": [], "next_cursor": "c2", "has_more": False})
            return httpx.Response(200, json={"added": [], "modified": [], "removed": [{"transaction_id": "t1"}],
                                             "next_cursor": "c3", "has_more": False})
        if p == "/investments/holdings/get":
            return httpx.Response(400, json={"error_type": "ITEM_ERROR", "error_code": "PRODUCTS_NOT_SUPPORTED",
                                             "error_message": "nope", "request_id": "r1"})
        if p == "/investments/transactions/get":
            return httpx.Response(400, json={"error_type": "ITEM_ERROR", "error_code": "NO_INVESTMENT_ACCOUNTS",
                                             "error_message": "nope", "request_id": "r2"})
        if p == "/liabilities/get":
            return httpx.Response(200, json={"liabilities": {"credit": [
                {"account_id": "a2", "last_statement_balance": 40.0, "minimum_payment_amount": 25.0,
                 "next_payment_due_date": "2026-11-01", "aprs": [{"apr_percentage": 24.9}]}]}})
        if p == "/item/get":
            return httpx.Response(200, json={"item": {"billed_products": ["transactions"],
                                                      "available_products": ["investments"]}, "status": {}})
        return httpx.Response(500, json={"error_code": "UNEXPECTED_PATH"})
    return httpx.MockTransport(handler)


@pytest.fixture
def env(tmp_path: Path):
    key = tmp_path / "token.key"
    create_key(key)
    s = Settings(plaid_env="sandbox", client_id="cid", secret=SECRET, db_path=tmp_path / "w.db",
                 key_path=key, allowed_users=frozenset({"user@example.com"}))
    calls: list = []
    return s, PlaidClient(s, transport=fake_plaid(calls)), calls


def test_secret_not_in_repr(env):
    s, _, _ = env
    assert SECRET not in repr(s)


def test_key_file_mode(tmp_path):
    k = tmp_path / "k"
    assert create_key(k) is True
    assert create_key(k) is False
    assert oct(k.stat().st_mode & 0o777) == "0o640"


def test_sync_and_report(env):
    s, plaid, calls = env
    db, box = DB(s.db_path), TokenBox(s.key_path)
    db.add_item("item1", box.seal(TOKEN), "ins_1", "Test Bank", "bank")
    # token is encrypted at rest
    raw = s.db_path.read_bytes()
    assert TOKEN.encode() not in raw
    res = Syncer(db, plaid, box).sync_item("item1")
    # bank Items never ask for investments (would be ADDITIONAL_CONSENT_REQUIRED noise)
    assert res == {"accounts": "ok", "transactions": "ok", "liabilities": "ok"}
    assert not any(p.startswith("/investments") for p, _ in calls)
    assert db.one("SELECT COUNT(*) n FROM transactions")["n"] == 2
    assert db.one("SELECT transactions_cursor c FROM items")["c"] == "c2"
    # each sync records a daily balance snapshot (one row per account per day)
    snaps = db.q("SELECT current FROM balance_snapshots ORDER BY current")
    assert [r["current"] for r in snaps] == [50.0, 100.0]
    # second sync uses cursor and applies removals
    Syncer(db, plaid, box).sync_item("item1")
    assert len(db.q("SELECT 1 FROM balance_snapshots")) == 2  # same day: upserted
    assert [r["transaction_id"] for r in db.q("SELECT transaction_id FROM transactions")] == ["t2"]
    md = report.to_markdown(report.build(db), "sandbox")
    for leak in ("100.0", "2000", "0000", "1111", "COFFEE", "PAYROLL", TOKEN, SECRET):
        assert leak not in md, leak
    assert "Test Bank" in md and "| yes |" in md


def test_investment_kind_skips_liabilities_and_prunes_stale(env):
    s, plaid, calls = env
    db, box = DB(s.db_path), TokenBox(s.key_path)
    db.add_item("inv1", box.seal(TOKEN), "ins_2", "Test Broker", "investment")
    db.set_result("inv1", "liabilities", "not_supported", "INVALID_INPUT/ADDITIONAL_CONSENT_REQUIRED: old")
    calls.clear()
    res = Syncer(db, plaid, box).sync_item("inv1")
    assert set(res) == {"accounts", "transactions", "investments", "investment_transactions"}
    assert "/liabilities/get" not in [p for p, _ in calls]
    prods = {r["product"] for r in db.q("SELECT product FROM sync_results WHERE item_id='inv1'")}
    assert "liabilities" not in prods


def test_ledger_survives_item_delete(env):
    s, _, _ = env
    db, box = DB(s.db_path), TokenBox(s.key_path)
    db.add_item("i1", box.seal("t"), None, "A", "bank")
    db.conn.execute("DELETE FROM items")
    assert db.items_ever_created() == 1


def test_app_boots_unconfigured(tmp_path: Path, monkeypatch) -> None:
    # A fresh box: config dir exists, plaid.env does not. The service must
    # still boot and say what is missing instead of crash-looping.
    monkeypatch.setenv("WEYLAND_CONFIG_DIR", str(tmp_path))
    for var in ("PLAID_CLIENT_ID", "PLAID_SECRET_SANDBOX", "PLAID_SECRET_PRODUCTION"):
        monkeypatch.delenv(var, raising=False)
    c = TestClient(create_app())
    r = c.get("/healthz")
    assert r.status_code == 200 and "unconfigured" in r.text and "PLAID_CLIENT_ID" in r.text
    page = c.get("/")
    assert page.status_code == 200 and "plaid.env" in page.text
    r = c.get("/api/state")
    assert r.status_code == 503 and r.json()["error"] == "unconfigured"
    assert c.post("/api/sync").status_code == 503


def test_months_503_on_unresolvable_waterfall_config(env, tmp_path):
    # A config that parses but whose selectors match no synced account must
    # surface as a 503 with detail on /api/months (was: raw 500), and on close.
    s, plaid, _ = env
    (tmp_path / "wf.toml").write_text(
        '[accounts]\nhysa = "Bank:9999"\n\n[roth_limits]\n2026 = 7500\n')
    c = TestClient(create_app(s, plaid, waterfall_path=tmp_path / "wf.toml"))
    h = {"tailscale-user-login": "user@example.com", "x-weyland": "1"}
    r = c.get("/api/months", headers=h)
    assert r.status_code == 503, r.text
    assert "matched 0" in r.json()["detail"]
    r = c.post("/api/months/2026-09/close", headers=h)
    assert r.status_code == 503 and "matched 0" in r.json()["detail"]


def test_web_auth_and_flow(env):
    s, plaid, calls = env
    c = TestClient(create_app(s, plaid))
    assert c.get("/healthz").text == "ok"
    assert c.get("/").status_code == 403
    assert c.get("/", headers={"tailscale-user-login": "evil@example.com"}).status_code == 403
    h = {"tailscale-user-login": "User@Example.com"}
    assert "sandbox" in c.get("/", headers=h).text
    # CSRF-style guard
    assert c.post("/api/link-token", json={"kind": "bank"}, headers=h).status_code == 400
    h2 = {**h, "x-weyland": "1"}
    r = c.post("/api/link-token", json={"kind": "bank"}, headers=h2)
    assert r.status_code == 200 and r.json()["link_token"] == "link-sandbox-x"
    sent = [b for p, b in calls if p == "/link/token/create"][-1]
    assert sent["products"] == ["transactions"] and "auth" not in json.dumps(sent)
    assert "@" not in sent["user"]["client_user_id"]  # Plaid rejects emails here
    r = c.post("/api/exchange", json={"public_token": "pub", "kind": "bank",
                                      "institution": {"institution_id": "ins_1", "name": "Test Bank"}}, headers=h2)
    assert r.status_code == 200, r.text
    st = c.get("/api/state", headers=h).json()
    assert st["items_ever_created"] == 1
    assert TOKEN not in json.dumps(st) and SECRET not in json.dumps(st)


def test_exchange_refuses_duplicate_institution(env):
    s, plaid, calls = env
    db, box = DB(s.db_path), TokenBox(s.key_path)
    db.add_item("dup1", box.seal(TOKEN), "ins_1", "Test Bank", "bank")
    c = TestClient(create_app(s, plaid))
    h = {"tailscale-user-login": "user@example.com", "x-weyland": "1"}
    body = {"public_token": "pub", "kind": "bank",
            "institution": {"institution_id": "ins_1", "name": "Test Bank"}}
    r = c.post("/api/exchange", json=body, headers=h)
    assert r.status_code == 409 and "Re-connect" in r.json()["detail"]
    # refused before the exchange: no Plaid call, no new row
    assert "/item/public_token/exchange" not in [p for p, _ in calls]
    assert db.one("SELECT COUNT(*) n FROM items")["n"] == 1
    # explicit override allows a genuine second login at the same institution
    calls.clear()
    r = c.post("/api/exchange", json={**body, "override_duplicate": True}, headers=h)
    assert r.status_code == 200, r.text
    assert "/item/public_token/exchange" in [p for p, _ in calls]
    assert db.one("SELECT COUNT(*) n FROM items")["n"] == 2


def test_production_guard(env, tmp_path):
    s, plaid, _ = env
    sp = Settings(plaid_env="production", client_id="cid", secret=SECRET, db_path=tmp_path / "p.db",
                  key_path=s.key_path, allowed_users=frozenset())
    db = DB(sp.db_path)
    box = TokenBox(sp.key_path)
    for i in range(8):
        db.add_item(f"i{i}", box.seal("t"), None, "X", "bank")
    c = TestClient(create_app(sp, plaid))
    r = c.post("/api/link-token", json={"kind": "bank"},
               headers={"tailscale-user-login": "a@b.c", "x-weyland": "1"})
    assert r.status_code == 409

"""Demo mode: made-up data that has to survive the real close pipeline."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from weyland import demo, report
from weyland.classify import classify, load_config
from weyland.close import months
from weyland.dashboard import overview, rewards, spending
from weyland.db import DB
from weyland.match import needs_review

# A few awkward "todays": month start, inside the close grace period, a tax-year
# boundary (Roth window), a leap day, and New Year's Eve.
DAYS = [date(2026, 11, 3), date(2026, 3, 1), date(2026, 4, 2), date(2028, 2, 29), date(2026, 12, 31)]


@pytest.mark.parametrize("today", DAYS, ids=str)
def test_demo_builds_and_passes_the_close_checks(tmp_path, today):
    s, wf = demo.build(tmp_path, today)
    db, cfg = DB(s.db_path), load_config(wf)
    ms = months(db.conn, cfg, today)
    # five full months of history plus the current one; build() closed the oldest four
    assert [m["month"] for m in ms][-1] == demo._ym(demo._add_months(date(today.year, today.month, 1), -5))
    assert [m["status"] for m in ms[2:]] == ["closed"] * 4
    assert ms[0]["status"] == "open"
    # last month is either ready to close or only waiting out the grace period
    assert ms[1]["status"] == "closeable" or \
        ms[1]["blockers"] == [f"waiting {cfg.close_grace_days}-day grace for late postings"]
    rows = classify(db.conn, cfg, today=today)
    assert [r.name for r in rows if needs_review(r)] == ["VENMO CASHOUT", "MOBILE CHECK DEPOSIT"]
    # the stories the screenshots rely on
    assert sum(r.match_status == "refunded" for r in rows) == 2          # one full refund pair
    assert any(r.bonus_decision == "bonus" for r in rows)
    assert any(r.cls == "other_income" and "REWARDS" in r.name for r in rows)
    ov = overview(db.conn, cfg, report.build(db)["items"], today)
    assert ov["recommendation"] and not ov["recommendation"].get("error")
    assert ov["net_worth"]["history"] and ov["holdings"]["positions"]
    sp = spending(db.conn, cfg, (today - timedelta(days=89)).isoformat(), today.isoformat(), today=today)
    assert sp["rows"] and any(r["cashback"] for r in sp["rows"])
    assert all(c["estimated"] > 0 for c in rewards(db.conn, cfg, today=today)["cards"])


def test_demo_is_deterministic(tmp_path):
    a, _ = demo.build(tmp_path / "a", DAYS[0])
    b, _ = demo.build(tmp_path / "b", DAYS[0])
    q = "SELECT transaction_id, date, amount, name FROM transactions ORDER BY transaction_id"
    assert DB(a.db_path).q(q) == DB(b.db_path).q(q)


def test_demo_app_serves_without_auth_and_never_calls_plaid(tmp_path):
    c = TestClient(demo.app(tmp_path))
    page = c.get("/")
    assert page.status_code == 200 and '<span class="env demo">demo</span>' in page.text
    for ep in ("/api/state", "/api/overview", "/api/months", "/api/review", "/api/rewards", "/api/roth",
               "/api/spending?start=2026-01-01&end=2026-12-31"):
        assert c.get(ep).status_code == 200, ep
    assert c.get("/api/state").json()["plaid_env"] == "demo"
    h = {"x-weyland": "1"}
    for ep, body in (("/api/sync", {}), ("/api/link-token", {"kind": "bank"}),
                     ("/api/exchange", {"public_token": "x"}),
                     ("/api/items/demo-harbor/update-token", {}), ("/api/items/demo-harbor/reconnected", {})):
        r = c.post(ep, json=body, headers=h)
        assert r.status_code == 403 and "demo mode" in r.json()["detail"], ep
    # local edits still work, so visitors can click through a close
    assert c.post("/api/review/demo-tx-00001", json={"class": "spending"}, headers=h).status_code == 200


def test_demo_cli_needs_no_config(tmp_path, monkeypatch):
    from weyland import cli
    seen = {}
    monkeypatch.setenv("WEYLAND_CONFIG_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setattr(demo, "serve", lambda host, port, d: seen.update(host=host, port=port, d=d) or 0)
    assert cli.main(["demo", "--port", "9999", "--dir", str(tmp_path / "d")]) == 0
    assert seen == {"host": "127.0.0.1", "port": 9999, "d": str(tmp_path / "d")}


def test_parallel_page_load_requests_all_succeed(tmp_path):
    # The page fires its API calls at once and FastAPI runs sync routes on a thread
    # pool, all sharing one sqlite connection. Unserialised, that raced
    # (sqlite3.InterfaceError / IndexError in production, "unavailable" in the UI).
    from concurrent.futures import ThreadPoolExecutor
    c = TestClient(demo.app(tmp_path))
    eps = ["/api/state", "/api/overview", "/api/months", "/api/review", "/api/roth"] * 6
    with ThreadPoolExecutor(len(eps)) as ex:
        codes = list(ex.map(lambda ep: c.get(ep).status_code, eps))
    assert codes == [200] * len(eps)

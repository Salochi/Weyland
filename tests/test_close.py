import json
from datetime import date

import pytest

from weyland.classify import parse_config, set_override
from weyland.close import CloseError, close_month, figures, months, reopen_month
from weyland.classify import classify

from test_classify import CFG, _db, _tx

CFG3 = {**CFG, "categories": {"rent": ["^ACME PROPERTY"]}}


def cfg():
    return parse_config(CFG3)


def seed(db):
    # history starts 2026-08-01 on both institutions
    _tx(db, "pay1", "hysa", "2026-08-07", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "pay2", "hysa", "2026-08-21", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "int", "hysa", "2026-08-31", -10, "Interest Earned", "INCOME", "INCOME_INTEREST_EARNED")
    _tx(db, "gift", "hysa", "2026-08-06", -500, "Check Deposit", "TRANSFER_IN", "TRANSFER_IN_DEPOSIT")
    _tx(db, "rent", "hysa", "2026-08-31", 1800, "ACME PROPERTY RENT", "RENT_AND_UTILITIES", "")
    _tx(db, "rm", "chk", "2026-08-01", -900, "PAT ROOMMATE ZELLE", "TRANSFER_IN", "")
    _tx(db, "food", "cucc", "2026-08-01", 300, "GROCER", "FOOD_AND_DRINK", "")
    _tx(db, "couch", "cucc", "2026-08-18", 400, "BELFORT FURN 123", "HOME_IMPROVEMENT", "")
    _tx(db, "couch_back", "cucc", "2026-09-15", -400, "BELFORT FURN", "HOME_IMPROVEMENT", "")
    _tx(db, "part_back", "cucc", "2026-08-20", -50, "GROCER", "FOOD_AND_DRINK", "")
    _tx(db, "cardpay", "hysa", "2026-08-25", 250, "CU ACH", "TRANSFER_OUT", "")
    _tx(db, "cardpay_in", "cucc", "2026-08-26", -250, "ACH PAYMENT RECEIVED", "LOAN_PAYMENTS", "")
    db.conn.execute("UPDATE accounts SET current = 24000, updated_at = '2026-10-01T12:00:00+00:00' "
                    "WHERE account_id = 'hysa'")
    set_override(db.conn, "gift", "other_income", "mom")


def test_figures_exclude_transfers_and_net_refunds(tmp_path):
    db = _db(tmp_path)
    seed(db)
    f = figures(classify(db.conn, cfg(), "2026-08", today=date(2026, 10, 1)))
    assert f["paychecks"] == 4000 and f["interest"] == 10 and f["other_income"] == 500
    # 300 food + 1800 rent - 50 partial refund - 900 roommate; Belfort pair and card payment excluded
    assert f["purchases"] == 2100 and f["refunds"] == 50 and f["reimbursements"] == 900
    # spending = what was bought (300 - 50); fixed = rent minus the roommate's share
    assert f["spending"] == 250 and f["fixed"] == 900 and f["subscriptions"] == 0
    assert f["outflows"] == 1150 and f["surplus"] == 2850
    assert f["categories"]["rent"] == 1800 and f["categories"]["food_and_drink"] == 250
    assert f["categories"]["reimbursed"] == -900


def test_bonus_candidate_stays_out_of_surplus(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "pay0", "hysa", "2026-07-24", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "bonus", "hysa", "2026-08-28", -6000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    f = figures(classify(db.conn, cfg(), "2026-08", today=date(2026, 10, 1)))
    assert f["paychecks"] == 4000 and f["bonus_candidates"] == 6000 and f["surplus"] == 2850


def test_close_gating(tmp_path):
    db = _db(tmp_path)
    seed(db)
    c = cfg()
    oct3 = {m["month"]: m for m in months(db.conn, c, date(2026, 9, 2))}
    assert oct3["2026-08"]["status"] == "open" and "grace" in oct3["2026-08"]["blockers"][0]
    assert "month not over" in oct3["2026-09"]["blockers"]
    with pytest.raises(CloseError, match="grace"):
        close_month(db.conn, c, "2026-08", date(2026, 9, 2))
    _tx(db, "weird", "hysa", "2026-08-12", 75, "SOMEONE", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    m = {m["month"]: m for m in months(db.conn, c, date(2026, 9, 10))}["2026-08"]
    assert m["status"] == "open" and m["blockers"] == ["1 transaction(s) need review"]
    set_override(db.conn, "weird", "spending", "copay")
    m = {m["month"]: m for m in months(db.conn, c, date(2026, 9, 10))}["2026-08"]
    assert m["status"] == "closeable"


def test_awaiting_transfer_blocks_close(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "late_pay", "hysa", "2026-08-31", 120, "CU ACH", "TRANSFER_OUT", "")
    m = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 9, 4))}["2026-08"]
    assert m["blockers"] == ["1 transfer(s) awaiting the other side"]


def test_close_snapshot_balance_and_drift(tmp_path):
    db = _db(tmp_path)
    seed(db)
    c = cfg()
    today = date(2026, 9, 10)
    _tx(db, "sep_int", "hysa", "2026-09-02", -15, "Interest Earned", "INCOME", "INCOME_INTEREST_EARNED")
    # Aug 31 balance = 24000 now, minus the Sept 2 deposit that came in after
    assert close_month(db.conn, c, "2026-08", today) == {
        "month": "2026-08", "hysa_balance": 23985, "balance_as_of": "2026-08-31", "recommendation": None}
    with pytest.raises(CloseError, match="already closed"):
        close_month(db.conn, c, "2026-08", today)
    m = {m["month"]: m for m in months(db.conn, c, today)}["2026-08"]
    assert m["status"] == "closed" and m["hysa_balance"] == 23985 and m["drift"] == {}
    # a late-posting purchase lands in the closed month
    _tx(db, "late", "cucc", "2026-08-30", 40, "GROCER", "FOOD_AND_DRINK", "")
    m = {m["month"]: m for m in months(db.conn, c, today)}["2026-08"]
    assert m["drift"]["spending"] == {"frozen": 250, "now": 290}
    assert m["drift"]["outflows"] == {"frozen": 1150, "now": 1190}
    assert m["frozen"]["spending"] == 250  # history not rewritten
    reopen_month(db.conn, "2026-08")
    close_month(db.conn, c, "2026-08", today)
    snap = db.conn.execute("SELECT figures_json FROM month_closes WHERE month='2026-08'").fetchone()[0]
    assert json.loads(snap)["spending"] == 290 and json.loads(snap)["outflows"] == 1190
    with pytest.raises(CloseError, match="not closed"):
        reopen_month(db.conn, "2026-07")


def _snap(db, d, bal, acct="hysa"):
    db.conn.execute("INSERT INTO balance_snapshots(account_id, date, current, at) VALUES (?,?,?,?)",
                    (acct, d, bal, d + "T12:00:00+00:00"))


def test_month_end_balance_replays_transactions_backwards(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "s1", "hysa", "2026-09-05", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "s2", "hysa", "2026-10-01", 300, "CU ACH", "TRANSFER_OUT", "")
    _tx(db, "s3", "hysa", "2026-09-30", 100, "pending thing", "TRANSFER_OUT", "", pending=1)
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 24))}
    # 24000 now; Oct 1 -300 out undone (+300); Sept 5 +2000 in undone (-2000); pending ignored
    assert ms["2026-09"]["month_end_balance"]["amount"] == 24300
    assert ms["2026-08"]["month_end_balance"] == {"date": "2026-08-31", "amount": 22300, "mismatches": []}
    assert "2026-07" not in ms  # no data, no month
    assert ms["2026-10"]["month_end_balance"] is None  # month not over
    # closing an old month later still records its own month-end balance, not today's
    r = close_month(db.conn, cfg(), "2026-08", date(2026, 10, 24))
    assert r["hysa_balance"] == 22300 and r["balance_as_of"] == "2026-08-31"


def test_balance_snapshots_check_the_reconstruction(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "s1", "hysa", "2026-09-05", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _snap(db, "2026-09-01", 22000.40)       # within $1 of the Aug 31 / Sept 1 reconstruction
    _snap(db, "2026-09-05", 22000)          # sync ran before the paycheck posted: matches day before
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 24))}
    assert ms["2026-08"]["month_end_balance"]["mismatches"] == []
    assert ms["2026-09"]["month_end_balance"]["mismatches"] == []
    _snap(db, "2026-09-20", 25000)          # a transaction is missing somewhere
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 24))}
    mm = ms["2026-09"]["month_end_balance"]["mismatches"]
    assert mm == [{"date": "2026-09-20", "reported": 25000, "reconstructed": 24000}]
    assert ms["2026-09"]["status"] == "open" and "balance check failed" in ms["2026-09"]["blockers"][0]
    assert ms["2026-08"]["status"] == "closeable"
    with pytest.raises(CloseError, match="balance check"):
        close_month(db.conn, cfg(), "2026-09", date(2026, 10, 24))


def test_balance_check_allows_two_day_weekend_lag(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "s1", "hysa", "2026-09-05", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    # balance before the Sept 5 paycheck is 22000; a snapshot two days later still shows it
    _snap(db, "2026-09-06", 22000)          # matches d-2 (Sept 4): posting lag, not a missing txn
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 24))}
    assert ms["2026-09"]["month_end_balance"]["mismatches"] == []
    _snap(db, "2026-09-07", 22000)          # three days stale: still flagged
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 24))}
    assert [x["date"] for x in ms["2026-09"]["month_end_balance"]["mismatches"]] == ["2026-09-07"]
    assert "balance check failed" in ms["2026-09"]["blockers"][0]


def test_card_balance_check_allows_two_day_lag(tmp_path):
    from weyland.close import BalanceReplay
    db = _db(tmp_path)
    seed(db)
    acct = db.conn.execute("SELECT account_id FROM accounts ORDER BY account_id LIMIT 1").fetchone()[0]
    db.conn.execute("UPDATE accounts SET current=500 WHERE account_id=?", (acct,))
    _tx(db, "c1", acct, "2026-09-10", 200, "GROCER", "FOOD_AND_DRINK", "")  # owed 300 before, 500 after
    _snap(db, "2026-09-10", 300, acct)      # d-1 match (Sept 9, before the purchase)
    _snap(db, "2026-09-11", 300, acct)      # d-2 match
    _snap(db, "2026-09-12", 300, acct)      # d-3: flagged
    rp = BalanceReplay(db.conn, acct, credit=True)
    assert [m["date"] for m in rp.mismatches("2026-09-10", "2026-09-12")] == ["2026-09-12"]


def test_closed_balance_drift(tmp_path):
    db = _db(tmp_path)
    seed(db)
    today = date(2026, 10, 24)
    close_month(db.conn, cfg(), "2026-08", today)
    # a September transaction shows up late: Aug 31's reconstructed balance changes
    _tx(db, "late_in", "hysa", "2026-09-03", -50, "Interest Earned", "INCOME", "INCOME_INTEREST_EARNED")
    m = {m["month"]: m for m in months(db.conn, cfg(), today)}["2026-08"]
    assert m["drift"] == {"hysa_balance": {"frozen": 24000, "now": 23950}}


def test_partial_history_flag(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "early", "cucc", "2026-07-01", 20, "GROCER", "FOOD_AND_DRINK", "")
    ms = {m["month"]: m for m in months(db.conn, cfg(), date(2026, 10, 1))}
    # bank (i1) history begins 2026-08-01, after July 1 -> July is partial and can't close
    assert ms["2026-07"]["partial_history"] and ms["2026-07"]["partial_institutions"] == ["i1"]
    assert ms["2026-07"]["status"] == "open" and "partial history" in ms["2026-07"]["blockers"][0]
    assert ms["2026-08"]["partial_history"] is False and ms["2026-08"]["status"] == "closeable"


def test_months_api(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
    import weyland.close as close_mod
    from weyland.config import Settings
    from weyland.crypto import create_key
    from weyland.plaid_client import PlaidClient
    from weyland.web import create_app

    key = tmp_path / "token.key"
    create_key(key)
    s = Settings(plaid_env="sandbox", client_id="cid", secret="s" * 30, db_path=tmp_path / "t.db",
                 key_path=key, allowed_users=frozenset({"user@example.com"}))
    db = _db(tmp_path)
    seed(db)
    cfgp = tmp_path / "waterfall.toml"
    cfgp.write_text('[accounts]\nhysa = "Bank:1111"\nchecking = "Bank:2222"\ncard = ["Bank:3333", "CU:Rewards Card"]\n'
                    'ignore = ["CU:4444"]\n[rules]\nemployer = ["^ACME CORP"]\nreimbursers = ["^PAT ROOMMATE"]\n'
                    'card_payment = ["^CU ACH"]\n')
    real = close_mod.months
    monkeypatch.setattr(close_mod, "months", lambda conn, cfg, today=None: real(conn, cfg, date(2026, 9, 10)))
    c = TestClient(create_app(s, PlaidClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                              waterfall_path=cfgp))
    h = {"tailscale-user-login": "user@example.com"}
    assert c.get("/api/months", headers={"tailscale-user-login": "evil@x.y"}).status_code == 403
    ms = {m["month"]: m for m in c.get("/api/months", headers=h).json()["months"]}
    assert ms["2026-08"]["status"] == "closeable" and ms["2026-08"]["figures"]["outflows"] == 1150
    assert c.post("/api/months/2026-08/close", json={}, headers=h).status_code == 400  # same-origin header
    hw = {**h, "x-weyland": "1"}
    assert c.post("/api/months/2026-08/close", json={}, headers=hw).status_code == 200
    assert c.post("/api/months/2026-08/close", json={}, headers=hw).status_code == 409
    assert c.post("/api/months/2026-09/close", json={}, headers=hw).status_code == 409
    assert c.post("/api/months/2026-08/reopen", json={}, headers=hw).json() == {"ok": True}

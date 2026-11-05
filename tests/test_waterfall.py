from datetime import date

import pytest

from weyland.classify import WaterfallConfigError, classify, parse_config, set_override
from weyland.close import BalanceReplay, CloseError, close_month, figures, months, reopen_month
from weyland.waterfall import recommend, tax_year, window_from

from test_classify import CFG, _db, _tx
from test_close import seed

WF = {**CFG, "waterfall": {"hysa_floor": 20000, "start": "2026-08"},
      "earmark": [{"name": "couch", "amount": 1000, "status": "open", "due": "2026-11"},
                  {"name": "old", "amount": 500, "status": "released"}]}


def cfg(**over):
    return parse_config({**WF, **over})


def _contrib(db, d, amount, ty):
    db.conn.execute("INSERT INTO roth_contributions(date, amount, tax_year, note, at) VALUES (?,?,?,?,?)",
                    (d, amount, ty, "", "now"))


def _skip(db, month):
    db.conn.execute("INSERT INTO roth_skips(month, at) VALUES (?, 'now')", (month,))


def test_tax_year_window():
    assert tax_year("2026-04") == 2026 and tax_year("2027-03") == 2026 and tax_year("2026-12") == 2026
    assert window_from("2026-10") == ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"]
    assert window_from("2027-03") == ["2027-03"]
    assert len(window_from("2026-04")) == 12


def test_config_earmarks_and_start():
    c = cfg()
    assert [e["name"] for e in c.open_earmarks] == ["couch"] and c.start == "2026-08"
    with pytest.raises(WaterfallConfigError, match="YYYY-MM"):
        cfg(waterfall={"start": "Oct"})
    with pytest.raises(WaterfallConfigError, match="numeric amount"):
        cfg(earmark=[{"name": "x"}])
    with pytest.raises(WaterfallConfigError, match="open or released"):
        cfg(earmark=[{"name": "x", "amount": 1, "status": "paid"}])


def test_investable_and_installment(tmp_path):
    db = _db(tmp_path)
    _contrib(db, "2026-05-01", 1000, 2026)
    _contrib(db, "2026-11-05", 500, 2026)      # after October ends: not counted for October
    _contrib(db, "2026-05-02", 9999, 2025)     # other tax year
    r = recommend(db.conn, cfg(), "2026-10", 26000, {"a": 700, "b": -50}, 0)
    # 26000 - 20000 floor - 1000 open earmark (released one ignored) - 700 owed (credit card in credit = 0)
    assert (r["earmarks_total"], r["cards_owed"], r["investable"]) == (1000, 700, 4300)
    ro = r["roth"]
    assert (ro["contributed"], ro["remaining"], ro["closes_left"]) == (1000, 6500, 6)
    assert r["to_roth"] == 1083 and r["to_brokerage"] == 3217 and r["shortfall"] == 0   # 6500/6 rounded down
    # November sees the Nov 5 contribution
    n = recommend(db.conn, cfg(), "2026-11", 26000, {}, 0)
    assert (n["roth"]["contributed"], n["roth"]["closes_left"], n["to_roth"]) == (1500, 5, 1200)


def test_split_installments_never_exceed_remaining():
    from weyland.waterfall import split_installments
    assert split_installments(5500, 6) == [916, 916, 916, 916, 916, 920]   # Oct 2026: no $917 months
    assert split_installments(6500, 5) == [1300] * 5
    assert split_installments(1000.50, 3) == [333, 333, 334.5]
    assert split_installments(0, 4) == [0, 0, 0, 0] and split_installments(100, 0) == []
    for rem in (7000, 6999.99, 5500, 1234.56, 3, 0.07):
        for n in range(1, 13):
            parts = split_installments(rem, n)
            assert round(sum(parts) * 100) == round(rem * 100)
            assert all(p == int(p) for p in parts[:-1]) and min(parts) >= 0


def test_last_open_close_takes_the_rest(tmp_path):
    db = _db(tmp_path)
    _contrib(db, "2026-05-01", 2000, 2026)            # 7500 limit -> 5500 left over Oct..Mar
    assert recommend(db.conn, cfg(), "2026-10", 30000, {}, 0)["to_roth"] == 916
    for i, d in enumerate(["2026-10-15", "2026-11-15", "2026-12-15", "2027-01-15", "2027-02-15"]):
        _contrib(db, d, 916, 2026)
    mar = recommend(db.conn, cfg(), "2027-03", 30000, {}, 0)
    assert mar["roth"]["closes_left"] == 1 and mar["to_roth"] == 920 and mar["roth"]["remaining"] == 920


def test_skip_month_holds_installment_in_hysa(tmp_path):
    db = _db(tmp_path)
    _contrib(db, "2026-05-01", 1000, 2026)
    _skip(db, "2026-10")
    r = recommend(db.conn, cfg(), "2026-10", 26000, {"a": 700}, 0)
    assert r["roth"]["skipped"] and r["to_roth"] == 0
    assert r["roth"]["closes_left"] == 5 and r["roth"]["installment"] == 1300   # what Nov–Mar will each get
    assert r["held_for_roth"] == 1083 and r["to_brokerage"] == 3217   # not dumped into brokerage
    n = recommend(db.conn, cfg(), "2026-11", 26000, {}, 0)
    assert n["roth"]["closes_left"] == 5 and n["to_roth"] == 1300           # 6500 / Nov–Mar
    _skip(db, "2027-01")
    assert recommend(db.conn, cfg(), "2026-11", 26000, {}, 0)["to_roth"] == 1625  # 6500 / 4


def test_bonus_and_shortfall_and_missing_limit(tmp_path):
    db = _db(tmp_path)
    _contrib(db, "2026-05-01", 1000, 2026)
    b = recommend(db.conn, cfg(), "2026-10", 26000, {"a": 700}, 4250)
    assert b["to_roth"] == 4250 and b["to_brokerage"] == 50              # bonus beats the installment
    big = recommend(db.conn, cfg(), "2026-10", 40000, {}, 9000)
    assert big["to_roth"] == 6500 and big["to_brokerage"] == 19000 - 6500  # capped at remaining limit
    poor = recommend(db.conn, cfg(), "2026-10", 20500, {"a": 700}, 0)
    assert poor["investable"] == 0 and poor["to_roth"] == 0 and poor["shortfall"] == 1083
    assert "no Roth limit" in recommend(db.conn, cfg(), "2027-05", 26000, {}, 0)["error"]
    assert recommend(db.conn, cfg(), "2026-10", None, {}, 0)["error"]


def test_card_balance_replay(tmp_path):
    db = _db(tmp_path)
    db.conn.execute("UPDATE accounts SET current = 300 WHERE account_id = 'cc'")
    _tx(db, "buy", "cc", "2026-09-10", 200, "SHOP")
    _tx(db, "pay", "cc", "2026-09-20", -500, "PAYMENT")
    rp = BalanceReplay(db.conn, "cc", credit=True)
    assert rp.at("2026-08-31") == 600 and rp.at("2026-09-15") == 800 and rp.at("2026-09-30") == 300


def test_months_recommendation_close_freezes_it(tmp_path):
    db = _db(tmp_path)
    seed(db)
    db.conn.execute("UPDATE accounts SET current = 150 WHERE account_id = 'cucc'")
    today = date(2026, 10, 24)
    m = {x["month"]: x for x in months(db.conn, cfg(), today)}["2026-08"]
    # cucc owed at Aug 31: 150 now, minus the Sept 15 Belfort refund (-400) undone => 550
    assert m["cards_owed"] == {"CU Rewards Card": 550, "Bank 3333": 0}
    r = m["recommendation"]
    assert r["hysa"] == 24000 and r["investable"] == 24000 - 20000 - 1000 - 550
    assert m["status"] == "closeable"
    assert months(db.conn, cfg(waterfall={"hysa_floor": 20000, "start": "2026-09"}), today)[1]["recommendation"] is None
    out = close_month(db.conn, cfg(), "2026-08", today)
    assert out["recommendation"]["to_roth"] == r["to_roth"]
    _contrib(db, "2026-08-15", 3000, 2026)      # recorded later: frozen recommendation unchanged
    m = {x["month"]: x for x in months(db.conn, cfg(), today)}["2026-08"]
    assert m["recommendation"] == out["recommendation"]
    reopen_month(db.conn, "2026-08")
    m = {x["month"]: x for x in months(db.conn, cfg(), today)}["2026-08"]
    assert m["recommendation"]["roth"]["contributed"] == 3000
    # a month closed before the start month froze no recommendation; never invent one later
    close_month(db.conn, cfg(waterfall={"hysa_floor": 20000, "start": "2026-09"}), "2026-08", today)
    m = {x["month"]: x for x in months(db.conn, cfg(), today)}["2026-08"]
    assert m["status"] == "closed" and m["recommendation"] is None


def test_bonus_candidate_needs_decision(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "pay0", "hysa", "2026-07-24", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "bonus", "hysa", "2026-08-28", -6000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    today = date(2026, 10, 24)
    get = lambda: {x["month"]: x for x in months(db.conn, cfg(), today)}["2026-08"]
    m = get()
    assert "possible bonus" in m["blockers"][-1] and m["status"] == "open"
    assert m["bonus_candidates"] == [{"transaction_id": "bonus", "date": "2026-08-28", "amount": 6000,
                                      "decision": None}]
    with pytest.raises(CloseError, match="bonus"):
        close_month(db.conn, cfg(), "2026-08", today)
    db.conn.execute("INSERT INTO bonus_decisions VALUES ('bonus', 'bonus', 'now')")
    m = get()
    assert m["figures"]["bonus"] == 6000 and m["figures"]["paychecks"] == 4000
    assert "possible bonus" not in " ".join(m["blockers"])
    assert m["recommendation"]["confirmed_bonus"] == 6000
    db.conn.execute("UPDATE bonus_decisions SET decision = 'paycheck'")
    f = get()["figures"]
    assert f["paychecks"] == 10000 and f["bonus"] == 0 and f["surplus"] == 10000 - f["outflows"]


def test_category_override(tmp_path):
    db = _db(tmp_path)
    _tx(db, "cash", "hysa", "2026-08-02", 1800, "Cash Withdrawal at Branch", "TRANSFER_OUT", "")
    set_override(db.conn, "cash", "spending", "first month rent", category="rent")
    r = classify(db.conn, cfg())[0]
    assert (r.cls, r.category) == ("spending", "rent")
    assert figures([r])["categories"] == {"rent": 1800}


def test_waterfall_api(tmp_path, monkeypatch):
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
    _tx(db, "pay0", "hysa", "2026-07-24", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "bonus", "hysa", "2026-08-28", -6000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    cfgp = tmp_path / "waterfall.toml"
    cfgp.write_text('[accounts]\nhysa = "Bank:1111"\nchecking = "Bank:2222"\ncard = ["Bank:3333", "CU:Rewards Card"]\n'
                    'ignore = ["CU:4444"]\n[rules]\nemployer = ["^ACME CORP"]\nreimbursers = ["^PAT ROOMMATE"]\n'
                    'card_payment = ["^CU ACH"]\n[waterfall]\nstart = "2026-08"\n[roth_limits]\n2026 = 7500\n')
    real = close_mod.months
    monkeypatch.setattr(close_mod, "months", lambda conn, cfg, today=None: real(conn, cfg, date(2026, 10, 24)))
    c = TestClient(create_app(s, PlaidClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                              waterfall_path=cfgp))
    h = {"tailscale-user-login": "user@example.com"}
    hw = {**h, "x-weyland": "1"}
    assert c.post("/api/roth", json={"date": "2026-05-01", "amount": 100}, headers=h).status_code == 400
    assert c.post("/api/roth", json={"date": "2026-05-01", "amount": -5}, headers=hw).status_code == 400
    assert c.post("/api/roth", json={"date": "2026-02-10", "amount": 100, "tax_year": 2024},
                  headers=hw).status_code == 400
    assert c.post("/api/roth", json={"date": "2027-02-10", "amount": 250}, headers=hw).json()["tax_year"] == 2026
    cid = c.post("/api/roth", json={"date": "2026-05-01", "amount": 1000, "note": "may"}, headers=hw).json()["id"]
    d = c.get("/api/roth", headers=h).json()
    assert d["years"] == [{"tax_year": 2026, "limit": 7500, "contributed": 1250}]
    assert c.post(f"/api/roth/{cid}/delete", json={}, headers=hw).json() == {"ok": True}
    assert c.post(f"/api/roth/{cid}/delete", json={}, headers=hw).status_code == 404
    assert c.post("/api/months/2026-11/skip-roth", json={"skip": True}, headers=hw).json() == {"ok": True}
    assert c.get("/api/roth", headers=h).json()["skips"] == ["2026-11"]
    assert c.post("/api/months/2026-11/skip-roth", json={"skip": False}, headers=hw).json() == {"ok": True}
    assert c.post("/api/months/26-11/skip-roth", json={"skip": True}, headers=hw).status_code == 400
    assert c.post("/api/bonus/bonus", json={"decision": "maybe"}, headers=hw).status_code == 400
    assert c.post("/api/bonus/nope", json={"decision": "bonus"}, headers=hw).status_code == 404
    assert c.post("/api/months/2026-08/close", json={}, headers=hw).status_code == 409   # undecided bonus
    assert c.post("/api/bonus/bonus", json={"decision": "bonus"}, headers=hw).json() == {"ok": True}
    assert c.post("/api/months/2026-08/close", json={}, headers=hw).status_code == 200
    assert c.post("/api/bonus/bonus", json={"decision": None}, headers=hw).status_code == 409  # month closed
    assert c.post("/api/months/2026-08/skip-roth", json={"skip": True}, headers=hw).status_code == 409
    ms = {m["month"]: m for m in c.get("/api/months", headers=h).json()["months"]}
    assert ms["2026-08"]["recommendation"]["confirmed_bonus"] == 6000

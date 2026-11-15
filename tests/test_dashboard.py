from datetime import date

import pytest

from weyland.classify import WaterfallConfigError, classify, parse_config
from weyland.close import close_month, figures, months
from weyland.dashboard import accounts, holdings, net_worth, net_worth_history, overview, roth_plan, spending

from test_classify import CFG, _db, _tx
from test_close import seed

V2 = {**CFG, "categories": {"rent": ["^ACME PROPERTY"], "subscriptions": ["^SPOTIFY"]},
      "waterfall": {"start": "2026-08"},
      "cards": {"limits": {"Bank:3333": 1000}, "manual_pay": ["CU:Rewards Card"]},
      "earmark": [{"name": "couch", "amount": 1000, "due": "2026-11", "note": "at delivery"}]}
TODAY = date(2026, 10, 24)


def cfg(**over):
    return parse_config({**V2, **over})


def _bal(db, aid, current, limit=None):
    db.conn.execute("UPDATE accounts SET current=?, credit_limit=? WHERE account_id=?", (current, limit, aid))


def _invest(db):
    db.conn.execute("INSERT INTO items(item_id, access_token_enc, institution_name, link_kind, created_at) "
                    "VALUES ('i3', x'00', 'Broker', 'investment', 'now')")
    for aid, sub, cur in (("brk", "brokerage", 5000), ("roth", "roth", 3000), ("k", "401k", 8000)):
        db.conn.execute("INSERT INTO accounts(account_id, item_id, name, type, subtype, mask, current) "
                        "VALUES (?, 'i3', ?, 'investment', ?, ?, ?)", (aid, sub.title(), sub, aid[:2], cur))
    db.conn.execute("INSERT INTO securities(security_id, ticker, name, type) VALUES "
                    "('s1', 'VTI', 'Total Market', 'etf'), ('s2', NULL, 'Cash', 'cash')")
    db.conn.execute("INSERT INTO holdings(account_id, security_id, item_id, institution_value, cost_basis) VALUES "
                    "('brk', 's1', 'i3', 4500, 4000), ('brk', 's2', 'i3', 500, NULL), ('roth', 's1', 'i3', 3000, 3200)")
    return {**V2, "accounts": {**V2["accounts"], "brokerage": "Broker:br", "roth": "Broker:ro",
                               "retirement": "Broker:k"}}


def test_config_spending_and_cards():
    c = cfg()
    assert c.fixed == ("rent", "rent_and_utilities", "insurance") and c.subscriptions == ("subscriptions",)
    assert c.card_limits == {"Bank:3333": 1000} and c.manual_pay == ("CU:Rewards Card",)
    with pytest.raises(WaterfallConfigError, match="both fixed and a subscription"):
        cfg(spending={"fixed": ["rent"], "subscriptions": ["rent"]})
    with pytest.raises(WaterfallConfigError, match="Institution:mask"):
        cfg(cards={"limits": {"3333": 5}})
    with pytest.raises(WaterfallConfigError, match="manual_pay"):
        cfg(cards={"manual_pay": ["nocolon"]})


def test_buckets_split_spending(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "spot", "cc", "2026-08-03", 12, "SPOTIFY USA", "ENTERTAINMENT", "")
    _tx(db, "ins", "cc", "2026-08-04", 90, "GEICO", "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE")
    _tx(db, "gpt", "cc", "2026-08-05", 20, "OPENAI *CHATGPT SUBSCR", "GENERAL_SERVICES", "")
    db.conn.execute("UPDATE transactions SET merchant_name = 'OpenAI' WHERE transaction_id = 'gpt'")
    rows = {r.transaction_id: r for r in classify(db.conn, cfg(), today=TODAY)}
    assert rows["gpt"].bucket == "spending"        # neither name nor merchant matches ^SPOTIFY...
    db.conn.execute("UPDATE transactions SET merchant_name = 'SPOTIFY' WHERE transaction_id = 'gpt'")
    rows = {r.transaction_id: r for r in classify(db.conn, cfg(), today=TODAY)}
    assert rows["gpt"].bucket == "subscriptions"   # ...until the merchant name does
    db.conn.execute("DELETE FROM transactions WHERE transaction_id = 'gpt'")
    rows = {r.transaction_id: r for r in classify(db.conn, cfg(), today=TODAY)}
    assert (rows["spot"].bucket, rows["ins"].bucket, rows["rent"].bucket, rows["food"].bucket) == \
        ("subscriptions", "fixed", "fixed", "spending")
    assert rows["rm"].bucket == ""     # reimbursements aren't purchases
    f = figures([r for r in rows.values() if r.date[:7] == "2026-08"])
    assert (f["spending"], f["subscriptions"], f["fixed"]) == (250, 12, 1800 + 90 - 900)
    assert f["outflows"] == 250 + 12 + 990 and f["surplus"] == 4000 - f["outflows"]


def test_legacy_close_compares_outflows_only(tmp_path):
    import json
    db = _db(tmp_path)
    seed(db)
    c = cfg(waterfall={})
    close_month(db.conn, c, "2026-08", TODAY)
    old = {k: v for k, v in json.loads(db.conn.execute("SELECT figures_json FROM month_closes").fetchone()[0]).items()
           if k not in ("subscriptions", "fixed", "outflows")}
    old["spending"] = 1150      # v1 stored the whole total as "spending"
    db.conn.execute("UPDATE month_closes SET figures_json=?", (json.dumps(old),))
    m = {x["month"]: x for x in months(db.conn, c, TODAY)}["2026-08"]
    assert m["drift"] == {}
    _tx(db, "late", "cucc", "2026-08-30", 40, "GROCER", "FOOD_AND_DRINK", "")
    m = {x["month"]: x for x in months(db.conn, c, TODAY)}["2026-08"]
    assert m["drift"] == {"outflows": {"frozen": 1150, "now": 1190}, "surplus": {"frozen": 2850, "now": 2810},
                          "purchases": {"frozen": 2100, "now": 2140}}


def test_accounts_limits_and_net_worth(tmp_path):
    db = _db(tmp_path)
    c = parse_config(_invest(db))
    _bal(db, "hysa", 25000); _bal(db, "chk", 1000); _bal(db, "cusav", 300)   # ignored account still counts
    _bal(db, "cc", 400); _bal(db, "cucc", -20, 5000)                         # card in credit counts as 0 owed
    db.conn.execute("INSERT INTO credit_liabilities(account_id, item_id, last_statement_balance, "
                    "minimum_payment, next_payment_due_date) VALUES ('cucc', 'i2', 120, 25, '2026-10-29')")
    a = {x["account_id"]: x for x in accounts(db.conn, c)}
    assert (a["cc"]["limit"], a["cc"]["limit_source"]) == (1000, "config")
    assert (a["cucc"]["limit"], a["cucc"]["limit_source"], a["cucc"]["manual_pay"]) == (5000, "plaid", True)
    assert a["cucc"]["due_date"] == "2026-10-29" and a["cusav"]["role"] == "ignore"
    nw = net_worth(list(a.values()))
    assert nw["parts"] == {"cash": 26300, "brokerage": 5000, "roth": 3000, "retirement": 8000, "cards": -400}
    assert nw["total"] == 26300 + 16000 - 400
    with pytest.raises(WaterfallConfigError, match="matched 0"):
        accounts(db.conn, parse_config({**V2, "cards": {"limits": {"Bank:9999": 1}}}))


def test_net_worth_history_waits_for_every_account(tmp_path):
    db = _db(tmp_path)
    for aid, cur in (("hysa", 100), ("chk", 10), ("cc", 5), ("cucc", 0), ("cusav", 1)):
        _bal(db, aid, cur)
    snaps = [("hysa", "2026-10-22", 90), ("hysa", "2026-10-23", 95), ("chk", "2026-10-23", 10), ("cc", "2026-10-23", 7),
             ("cucc", "2026-10-23", 0), ("cusav", "2026-10-23", 1), ("hysa", "2026-10-24", 100)]
    for aid, d, v in snaps:
        db.conn.execute("INSERT INTO balance_snapshots(account_id, date, current, at) VALUES (?,?,?,'now')", (aid, d, v))
    hist = net_worth_history(db.conn, accounts(db.conn, cfg()))
    # Oct 22 dropped (only the HYSA had reported); Oct 24 carries the others forward
    assert hist == [{"date": "2026-10-23", "total": 95 + 10 - 7 + 1}, {"date": "2026-10-24", "total": 100 + 10 - 7 + 1}]


def test_holdings(tmp_path):
    db = _db(tmp_path)
    c = parse_config(_invest(db))
    h = holdings(db.conn, accounts(db.conn, c))
    assert h["total"] == 8000 and [p["value"] for p in h["positions"]] == [4500, 3000, 500]
    assert [p["gain"] for p in h["positions"]] == [500, -200, None]
    assert h["positions"][0]["weight"] == 0.5625 and h["by_type"] == {"etf": 7500, "cash": 500}


def test_roth_plan_schedule(tmp_path):
    db = _db(tmp_path)
    db.conn.execute("INSERT INTO roth_contributions(date, amount, tax_year, note, at) VALUES "
                    "('2026-06-12', 2000, 2026, '', 'now'), ('2026-05-01', 999, 2025, '', 'now')")
    db.conn.execute("INSERT INTO roth_skips(month, at) VALUES ('2026-10', 'now')")
    p = roth_plan(db.conn, cfg(), TODAY)
    assert (p["contributed"], p["remaining"], p["closes_left"], p["installment"]) == (2000, 5500, 5, 1100)
    assert p["schedule"][0] == {"month": "2026-10", "skipped": True, "amount": 0.0}
    assert [s["month"] for s in p["schedule"]][-1] == "2027-03" and p["deadline"] == "2027-04-15"
    assert p["last"] == {"date": "2026-06-12", "amount": 2000}


def test_roth_plan_schedule_rounds_down_and_never_overshoots(tmp_path):
    db = _db(tmp_path)
    db.conn.execute("INSERT INTO roth_contributions(date, amount, tax_year, note, at) VALUES "
                    "('2026-05-01', 2000, 2026, '', 'now')")
    p = roth_plan(db.conn, cfg(), TODAY)
    amts = [s["amount"] for s in p["schedule"]]
    assert p["remaining"] == 5500 and amts == [916, 916, 916, 916, 916, 920] and p["installment"] == 916
    assert sum(amts) == p["remaining"]
    db.conn.execute("INSERT INTO roth_skips(month, at) VALUES ('2027-03', 'now')")
    amts = [s["amount"] for s in roth_plan(db.conn, cfg(), TODAY)["schedule"]]
    assert amts == [1100, 1100, 1100, 1100, 1100, 0.0]   # remainder lands on the last OPEN close
    assert roth_plan(db.conn, cfg(roth_limits={}), TODAY)["error"]


def test_overview_attention_and_upcoming(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _bal(db, "cucc", 300, 5000)
    db.conn.execute("INSERT INTO credit_liabilities(account_id, item_id, last_statement_balance, "
                    "minimum_payment, next_payment_due_date) VALUES ('cucc', 'i2', 120, 25, '2026-10-29')")
    _tx(db, "odd", "hysa", "2026-09-09", 75, "SOMEONE ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    items = [{"institution": "CU", "health": {"state": "reconnect", "reason": "Plaid reports ITEM_LOGIN_REQUIRED"},
              "last_sync": "2020-01-01T00:00:00+00:00"}]
    ov = overview(db.conn, cfg(), items, TODAY)
    texts = [a["text"] for a in ov["attention"]]
    assert texts[0] == "Rewards Card payment due in 5 day(s), no autopay"
    assert "1 transaction(s) to review" in texts and "CU: Plaid reports ITEM_LOGIN_REQUIRED" in texts
    assert any(t.startswith("last sync") for t in texts) and "2026-08 is ready to close" in texts
    kinds = [(u["kind"], u["date"]) for u in ov["upcoming"]]
    assert kinds == [("card", "2026-10-29"), ("earmark", "2026-11-01"), ("roth", "2027-04-15")]
    assert ov["next_close"]["month"] == "2026-08"      # closeable, date passed: not in upcoming
    # newest month with a recommendation (Sep: blocked by review, still previewed)
    assert ov["recommendation"]["month"] == "2026-09" and ov["recommendation"]["status"] == "open"
    assert [f["month"] for f in ov["cash_flow"]] == ["2026-08", "2026-09"]
    assert ov["cash_flow"][0]["fixed"] == 900 and ov["cash_flow"][0]["spending"] == 250
    # paid statement: no nag
    db.conn.execute("UPDATE credit_liabilities SET last_statement_balance = 0")
    assert not any("no autopay" in a["text"] for a in overview(db.conn, cfg(), [], TODAY)["attention"])


def test_spending_filters_and_signs(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "spot", "cc", "2026-08-03", 12, "SPOTIFY USA", "ENTERTAINMENT", "")
    c = cfg()
    s = spending(db.conn, c, "2026-08-01", "2026-08-31", today=TODAY)
    by = {r["transaction_id"]: r for r in s["rows"]}
    assert set(by) == {"food", "part_back", "couch"}           # spending bucket only, Belfort shown
    assert by["food"]["amount"] == -300 and by["part_back"]["amount"] == 50
    assert by["couch"]["refunded"] and s["refunded_pairs"] == 1
    assert (s["spent"], s["purchases"], s["refunds"]) == (250, 1, 50)
    assert s["categories"] == {"food_and_drink": 250}
    allb = spending(db.conn, c, "2026-08-01", "2026-08-31", ("spending", "subscriptions", "fixed"), today=TODAY)
    assert allb["spent"] == 250 + 12 + 1800 - 900 and allb["categories"]["reimbursed"] == -900
    assert {"rent", "subscriptions", "reimbursed"} <= set(allb["all_categories"])
    assert spending(db.conn, c, "2026-08-01", "2026-08-31", account="cc", today=TODAY)["rows"] == []
    assert [r["transaction_id"] for r in spending(db.conn, c, "2026-08-01", "2026-08-31", q="groc",
                                                  today=TODAY)["rows"]] == ["part_back", "food"]
    only = spending(db.conn, c, "2026-08-01", "2026-08-31", ("fixed",), category=("rent",), today=TODAY)
    assert [r["transaction_id"] for r in only["rows"]] == ["rent"]
    prev = spending(db.conn, c, "2026-09-01", "2026-09-30", today=TODAY)["previous"]
    # Aug 2-31: the Aug 1 grocery run is outside the window; only the partial refund is in it
    assert prev["start"] == "2026-08-02" and prev["end"] == "2026-08-31" and prev["spent"] == -50
    with pytest.raises(WaterfallConfigError, match="before start"):
        spending(db.conn, c, "2026-08-31", "2026-08-01")


def test_dashboard_api(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient
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
                    'card_payment = ["^CU ACH"]\n[categories]\nrent = ["^ACME PROPERTY"]\n[roth_limits]\n2026 = 7500\n'
                    '[cards]\nlimits = { "Bank:3333" = 1000 }\n')
    c = TestClient(create_app(s, PlaidClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(500))),
                              waterfall_path=cfgp))
    h = {"tailscale-user-login": "user@example.com"}
    assert c.get("/api/overview", headers={"tailscale-user-login": "x@y.z"}).status_code == 403
    ov = c.get("/api/overview", headers=h).json()
    assert {"accounts", "net_worth", "holdings", "cash_flow", "roth", "attention", "upcoming"} <= set(ov)
    assert next(a for a in ov["accounts"] if a["account_id"] == "cc")["limit"] == 1000
    sp = c.get("/api/spending?start=2026-08-01&end=2026-08-31&buckets=spending,fixed", headers=h).json()
    assert sp["spent"] == 250 + 900
    assert c.get("/api/spending?start=2026-08-01&end=2026-08-31&buckets=nope", headers=h).status_code == 400
    assert c.get("/api/spending?start=bad&end=2026-08-31", headers=h).status_code == 400
    assert c.get("/api/spending?start=2026-08-01", headers=h).status_code == 422
    page = c.get("/", headers=h).text
    assert 'id="ov"' in page and 'data-v="spending"' in page and "{{ENV}}" not in page


def _k401(db, broken=True):
    """401(k) on item i3 with two real contributions; its login broke after Aug 10."""
    from weyland.dashboard import retirement_estimates
    c = parse_config(_invest(db))
    meta = '{"item_error": "ITEM_LOGIN_REQUIRED"}' if broken else '{}'
    db.conn.execute("UPDATE items SET products_json=? WHERE item_id='i3'", (meta,))
    for tid, d, amt in (("c1", "2026-07-24", 150), ("c2", "2026-08-07", 160)):
        db.conn.execute("INSERT INTO investment_transactions(investment_transaction_id, account_id, item_id, date, "
                        "amount, type, subtype) VALUES (?, 'k', 'i3', ?, ?, 'buy', 'contribution')", (tid, d, amt))
    db.conn.execute("INSERT INTO investment_transactions(investment_transaction_id, account_id, item_id, date, "
                    "amount, type, subtype) VALUES ('f1', 'k', 'i3', '2026-08-08', 5, 'fee', 'miscellaneous fee')")
    db.conn.execute("INSERT INTO balance_snapshots(account_id, date, current, at) VALUES ('k', '2026-08-10', 8000, 'x')")
    accts = accounts(db.conn, c)
    retirement_estimates(db.conn, accts, classify(db.conn, c, today=TODAY))
    return c, {a["account_id"]: a for a in accts}


def test_half_synced_401k_repeats_last_contribution(tmp_path):
    db = _db(tmp_path)
    seed(db)  # paychecks Aug 7 and Aug 21
    _tx(db, "pay3", "hysa", "2026-09-04", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    c, a = _k401(db)
    e = a["k"]["estimate"]
    # Aug 7 paycheck is already in the last balance (snapshot Aug 10); Aug 21 + Sept 4 are estimated
    assert e["paychecks"] == ["2026-08-21", "2026-09-04"] and e["per_paycheck"] == 160
    assert e["basis_date"] == "2026-08-07" and e["as_of"] == "2026-08-10"
    assert e["current"] == 8000 + 2 * 160 and a["k"]["current"] == 8000  # real value untouched
    nw = net_worth(list(a.values()))
    assert nw["parts"]["retirement"] == 8320 and nw["estimated"] == [a["k"]["label"]]
    ov = overview(db.conn, c, [], TODAY)
    assert any("estimated, 2 paycheck contribution(s) added since 2026-08-10" in x["text"] for x in ov["attention"])
    assert ov["net_worth"]["parts"]["retirement"] == 8320


def test_half_synced_401k_off_when_healthy_or_bonus(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "pay3", "hysa", "2026-09-04", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _, a = _k401(db, broken=False)
    assert "estimate" not in a["k"]
    assert "estimated" not in net_worth(list(a.values()))


def test_half_synced_401k_skips_bonus_candidates(tmp_path):
    db = _db(tmp_path)
    seed(db)
    _tx(db, "big", "hysa", "2026-09-04", -9000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")  # undecided bonus
    _, a = _k401(db)
    assert a["k"]["estimate"]["paychecks"] == ["2026-08-21"]


def test_half_synced_401k_nothing_missed(tmp_path):
    db = _db(tmp_path)
    seed(db)
    db.conn.execute("DELETE FROM transactions WHERE transaction_id='pay2'")
    _, a = _k401(db)
    assert "estimate" not in a["k"]

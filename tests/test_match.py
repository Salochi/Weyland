from datetime import date

from weyland.classify import parse_config, classify, set_override, summarize
from weyland.match import needs_review

from test_classify import CFG, _db, _tx, by_id

TODAY = date(2026, 10, 24)


def run(db, month=None):
    return classify(db.conn, parse_config(CFG), month=month, today=TODAY)


def test_card_payment_pairs_across_institutions_and_months(tmp_path):
    db = _db(tmp_path)
    _tx(db, "hysa_out", "hysa", "2026-09-30", 200, "CU ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    _tx(db, "card_in", "cucc", "2026-10-02", -200, "ACH PAYMENT RECEIVED", "INCOME", "INCOME_SALARY")
    rows = by_id(run(db))
    assert rows["hysa_out"].match_status == rows["card_in"].match_status == "matched"
    assert rows["hysa_out"].match_with == "card_in" and rows["card_in"].match_with == "hysa_out"
    # month view still sees the pair that straddles the boundary
    assert run(db, "2026-10")[0].match_with == "hysa_out"


def test_closest_date_wins_and_each_leg_used_once(tmp_path):
    db = _db(tmp_path)
    _tx(db, "out1", "hysa", "2026-09-01", 100, "BANK", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    _tx(db, "out2", "hysa", "2026-09-05", 100, "BANK", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    _tx(db, "in1", "cc", "2026-09-02", -100, "PAYMENT", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    _tx(db, "in2", "cc", "2026-09-06", -100, "PAYMENT", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    rows = by_id(run(db))
    assert (rows["out1"].match_with, rows["out2"].match_with) == ("in1", "in2")


def test_internal_moves_pair_and_amount_or_window_mismatch_goes_to_review(tmp_path):
    db = _db(tmp_path)
    _tx(db, "mv_out", "chk", "2026-09-04", 600, "Withdrawal to Savings XXXX1111", "TRANSFER_OUT", "")
    _tx(db, "mv_in", "hysa", "2026-09-04", -600, "Deposit from Checking XXXX2222", "TRANSFER_IN", "")
    _tx(db, "odd_out", "chk", "2026-09-10", 50, "Withdrawal to Savings XXXX1111", "TRANSFER_OUT", "")
    _tx(db, "late_in", "hysa", "2026-09-20", -50, "Deposit from Checking XXXX2222", "TRANSFER_IN", "")
    rows = by_id(run(db))
    assert rows["mv_out"].match_with == "mv_in"
    assert rows["odd_out"].match_status == rows["late_in"].match_status == "unmatched"
    assert needs_review(rows["odd_out"]) and not needs_review(rows["mv_out"])
    assert summarize(list(rows.values()))["review"] == 2


def test_outside_history_and_awaiting_are_not_review(tmp_path):
    db = _db(tmp_path)
    _tx(db, "hysa_first", "hysa", "2026-08-01", 5, "COFFEE", "FOOD_AND_DRINK", "")
    _tx(db, "old_card_pay", "cucc", "2026-03-02", -150, "ACH PAYMENT RECEIVED", "LOAN_PAYMENTS", "")
    _tx(db, "fresh", "hysa", "2026-10-23", 80, "CU ACH", "TRANSFER_OUT", "")
    rows = by_id(run(db))
    assert rows["old_card_pay"].match_status == "outside_history"
    assert rows["old_card_pay"].cls == "card_payment"  # still never spending
    assert rows["fresh"].match_status == "awaiting"
    assert not any(needs_review(r) for r in rows.values())


def test_full_refund_pairs_with_purchase_partial_does_not(tmp_path):
    db = _db(tmp_path)
    _tx(db, "buy", "cc", "2026-08-18", 300, "BELFORT FURNITURE BASICS 7034067600", "HOME_IMPROVEMENT", "")
    _tx(db, "back", "cc", "2026-09-15", -300, "BELFORT FURNITURE BASICS", "HOME_IMPROVEMENT", "")
    _tx(db, "app", "cc", "2025-12-01", 10, "APPLE.COM/BILL", "GENERAL_MERCHANDISE", "")
    _tx(db, "app_part", "cc", "2025-12-23", -4, "APPLE.COM/BILL", "GENERAL_MERCHANDISE", "")
    _tx(db, "other_buy", "cucc", "2026-08-20", 300, "MACYS", "GENERAL_MERCHANDISE", "")
    rows = by_id(run(db))
    assert rows["buy"].match_status == rows["back"].match_status == "refunded"
    assert rows["back"].match_with == "buy"
    assert rows["app_part"].match_status == "unmatched" and not needs_review(rows["app_part"])
    assert rows["other_buy"].match_status == ""


def test_refund_never_pairs_with_a_later_purchase(tmp_path):
    db = _db(tmp_path)
    _tx(db, "back", "cc", "2026-09-01", -25, "TARGET STORE", "GENERAL_MERCHANDISE", "")
    _tx(db, "buy_after", "cc", "2026-09-05", 25, "TARGET STORE", "GENERAL_MERCHANDISE", "")
    rows = by_id(run(db))
    assert rows["back"].match_status == "unmatched" and rows["buy_after"].match_status == ""


def test_override_confirms_unpaired_transfer(tmp_path):
    db = _db(tmp_path)
    _tx(db, "hysa_first", "hysa", "2026-08-01", 5, "COFFEE", "FOOD_AND_DRINK", "")
    _tx(db, "card_first", "cucc", "2026-08-01", 5, "COFFEE", "FOOD_AND_DRINK", "")
    _tx(db, "lonely", "hysa", "2026-09-10", 40, "CU ACH", "TRANSFER_OUT", "")
    assert by_id(run(db))["lonely"].match_status == "unmatched"
    set_override(db.conn, "lonely", "card_payment", "paid from old account")
    assert by_id(run(db))["lonely"].match_status == "confirmed"


def test_review_api(tmp_path):
    from fastapi.testclient import TestClient
    from weyland.config import Settings
    from weyland.crypto import create_key
    from weyland.plaid_client import PlaidClient
    from weyland.web import create_app
    import httpx

    key = tmp_path / "token.key"
    create_key(key)
    s = Settings(plaid_env="sandbox", client_id="cid", secret="s" * 30, db_path=tmp_path / "t.db",
                 key_path=key, allowed_users=frozenset({"user@example.com"}))
    db = _db(tmp_path)
    _tx(db, "mystery", "hysa", "2026-09-08", 75, "SOMEONE ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    db.conn.commit()
    plaid = PlaidClient(s, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    h = {"tailscale-user-login": "user@example.com"}

    missing = TestClient(create_app(s, plaid, waterfall_path=tmp_path / "nope.toml"))
    assert missing.get("/api/review", headers=h).status_code == 503

    cfg = tmp_path / "waterfall.toml"
    cfg.write_text('[accounts]\nhysa = "Bank:1111"\nchecking = "Bank:2222"\ncard = ["Bank:3333", "CU:Rewards Card"]\n'
                   'ignore = ["CU:4444"]\n')
    c = TestClient(create_app(s, plaid, waterfall_path=cfg))
    assert c.get("/api/review", headers={"tailscale-user-login": "evil@x.y"}).status_code == 403
    rv = c.get("/api/review", headers=h).json()
    assert [q["transaction_id"] for q in rv["queue"]] == ["mystery"] and "unknown" not in rv["classes"]
    assert c.post("/api/review/mystery", json={"class": "spending"}, headers=h).status_code == 400  # no x-weyland
    hw = {**h, "x-weyland": "1"}
    assert c.post("/api/review/mystery", json={"class": "bogus"}, headers=hw).status_code == 400
    assert c.post("/api/review/mystery", json={"class": "spending", "note": "copay"}, headers=hw).json() == {"ok": True}
    assert c.get("/api/review", headers=h).json()["queue"] == []

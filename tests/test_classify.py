import sqlite3

import pytest

from weyland.classify import (WaterfallConfigError, classify, parse_config, resolve_roles,
                              summarize, unassigned_accounts)
from weyland.db import DB

CFG = {
    "accounts": {
        "hysa": "Bank:1111",
        "checking": "Bank:2222",
        "card": ["Bank:3333", "CU:Rewards Card"],
        "ignore": ["CU:4444"],
    },
    "rules": {
        "employer": ["^ACME CORP"],
        "reimbursers": ["^PAT ROOMMATE"],
        "card_payment": ["^CU ACH"],
    },
    "roth_limits": {"2026": 7500},
}


def _db(tmp_path):
    db = DB(tmp_path / "t.db")
    c = db.conn
    c.execute("INSERT INTO items(item_id, access_token_enc, institution_name, link_kind, created_at) VALUES "
              "('i1', x'00', 'Bank', 'bank', 'now'), ('i2', x'00', 'CU', 'bank', 'now')")
    for aid, item, name, typ, sub, mask in [
        ("hysa", "i1", "Savings", "depository", "savings", "1111"),
        ("chk", "i1", "Checking", "depository", "checking", "2222"),
        ("cc", "i1", "Card", "credit", "credit card", "3333"),
        ("cucc", "i2", "Rewards Card", "credit", "credit card", None),
        ("cusav", "i2", "Share Savings", "depository", "savings", "4444"),
    ]:
        c.execute("INSERT INTO accounts(account_id, item_id, name, type, subtype, mask) VALUES (?,?,?,?,?,?)",
                  (aid, item, name, typ, sub, mask))
    return db


def _tx(db, tid, acct, date, amount, name, prim="", det="", pending=0):
    item = "i2" if acct.startswith("cu") else "i1"
    db.conn.execute(
        "INSERT INTO transactions(transaction_id, account_id, item_id, date, amount, name, pending, "
        "category_primary, category_detail) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, acct, item, date, amount, name, pending, prim, det))


def by_id(rows):
    return {r.transaction_id: r for r in rows}


def test_roles_resolve_by_mask_or_name(tmp_path):
    db = _db(tmp_path)
    roles = resolve_roles(db.conn, parse_config(CFG))
    assert roles == {"hysa": "hysa", "chk": "checking", "cc": "card", "cucc": "card", "cusav": "ignore"}
    assert unassigned_accounts(db.conn, parse_config(CFG)) == []


def test_selector_survives_colon_in_institution_name(tmp_path):
    # e.g. the sandbox fixture institution "First Platypus Bank (sandbox: transactions)"
    db = _db(tmp_path)
    c = db.conn
    c.execute("INSERT INTO items(item_id, access_token_enc, institution_name, link_kind, created_at) VALUES "
              "('i3', x'00', 'First Platypus Bank (sandbox: transactions)', 'bank', 'now')")
    c.execute("INSERT INTO accounts(account_id, item_id, name, type, subtype, mask) VALUES "
              "('ppchk', 'i3', 'Checking', 'depository', 'checking', '1000')")
    from weyland.classify import select_account
    from weyland.dashboard import select_label
    sel = "First Platypus Bank (sandbox: transactions):1000"
    roles = resolve_roles(db.conn, parse_config({**CFG, "accounts": {**CFG["accounts"], "hysa": sel}}))
    assert roles["ppchk"] == "hysa"
    assert select_account(db.conn, sel) == "ppchk"
    assert select_label(db.conn, sel) == "Checking"


def test_bad_selector_and_roles_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(WaterfallConfigError, match="matched 0"):
        resolve_roles(db.conn, parse_config({**CFG, "accounts": {"hysa": "Bank:9999"}}))
    with pytest.raises(WaterfallConfigError, match="unknown account roles"):
        parse_config({**CFG, "accounts": {"hysa": "Bank:1111", "piggybank": "x:y"}})
    with pytest.raises(WaterfallConfigError, match="exactly one"):
        parse_config({**CFG, "accounts": {"card": "Bank:3333"}})
    with pytest.raises(WaterfallConfigError, match="bad regex"):
        parse_config({**CFG, "rules": {"employer": ["("]}})


def test_classes(tmp_path):
    db = _db(tmp_path)
    _tx(db, "pay", "hysa", "2026-09-12", -2000, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    _tx(db, "int", "hysa", "2026-09-30", -5, "Monthly Interest Paid", "INCOME", "INCOME_INTEREST_EARNED")
    _tx(db, "zelle", "chk", "2026-09-03", -600, "PAT ROOMMATE", "OTHER", "OTHER_OTHER")
    _tx(db, "mv1", "chk", "2026-09-04", 600, "Withdrawal to Savings XXXX1111", "TRANSFER_OUT", "TRANSFER_OUT_SAVINGS")
    _tx(db, "mv2", "hysa", "2026-09-04", -600, "Deposit from Checking XXXX2222", "TRANSFER_IN", "TRANSFER_IN_OTHER_TRANSFER_IN")
    _tx(db, "rent", "hysa", "2026-09-05", 1500, "RENT CO", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT")
    _tx(db, "ccpay", "hysa", "2026-09-20", 300, "BANK", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    _tx(db, "ccpay_in", "cc", "2026-09-20", -300, "AUTOPAY PYMT", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    _tx(db, "cupay", "hysa", "2026-09-21", 200, "CU ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    _tx(db, "cupay_in", "cucc", "2026-09-22", -200, "ACH PAYMENT RECEIVED", "INCOME", "INCOME_SALARY")
    _tx(db, "coffee", "cc", "2026-09-06", 5, "COFFEE", "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
    _tx(db, "return", "cucc", "2026-09-07", -40, "FURNITURE STORE", "HOME_IMPROVEMENT", "HOME_IMPROVEMENT_FURNITURE")
    _tx(db, "mystery", "hysa", "2026-09-08", 75, "SOMEONE ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    _tx(db, "cash", "hysa", "2026-09-09", 60, "Cash Withdrawal at Branch", "TRANSFER_OUT", "TRANSFER_OUT_WITHDRAWAL")
    _tx(db, "cudiv", "cusav", "2026-09-30", -0.02, "Dividend", "INCOME", "INCOME_DIVIDENDS")
    _tx(db, "pend", "cc", "2026-09-29", 9, "PENDING THING", "FOOD_AND_DRINK", "", pending=1)
    rows = by_id(classify(db.conn, parse_config(CFG)))
    expect = {
        "pay": "paycheck", "int": "interest", "zelle": "reimbursement",
        "mv1": "internal_transfer", "mv2": "internal_transfer", "rent": "spending",
        "ccpay": "card_payment", "ccpay_in": "card_payment", "cupay": "card_payment",
        "cupay_in": "card_payment",  # Plaid labels this INCOME_SALARY; it is a card payment
        "coffee": "spending", "return": "refund", "mystery": "unknown", "cash": "spending",
    }
    assert {k: v.cls for k, v in rows.items()} == expect  # credit-union savings ignored, pending skipped
    assert rows["mystery"].reason.endswith("needs review")


def test_overrides_win_and_month_filter(tmp_path):
    db = _db(tmp_path)
    _tx(db, "mystery", "hysa", "2026-09-08", 75, "SOMEONE ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    _tx(db, "oct", "cc", "2026-10-01", 5, "COFFEE", "FOOD_AND_DRINK", "")
    db.conn.execute("INSERT INTO txn_overrides(transaction_id, class, note, at) "
                    "VALUES ('mystery', 'spending', 'gift for mom', 'now')")
    rows = classify(db.conn, parse_config(CFG), month="2026-09")
    assert [(r.transaction_id, r.cls) for r in rows] == [("mystery", "spending")]
    assert rows[0].reason == "manual override: gift for mom"


def test_set_override_validates_and_clears(tmp_path):
    from weyland.classify import set_override
    db = _db(tmp_path)
    _tx(db, "mystery", "hysa", "2026-09-08", 75, "SOMEONE ACH", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
    with pytest.raises(WaterfallConfigError, match="no such transaction"):
        set_override(db.conn, "nope", "spending")
    with pytest.raises(WaterfallConfigError, match="class must be"):
        set_override(db.conn, "mystery", "unknown")
    set_override(db.conn, "mystery", "other_income", "first")
    set_override(db.conn, "mystery", "spending", "copay")  # upsert
    assert by_id(classify(db.conn, parse_config(CFG)))["mystery"].reason == "manual override: copay"
    set_override(db.conn, "mystery", None)
    assert by_id(classify(db.conn, parse_config(CFG)))["mystery"].cls == "unknown"


def test_bonus_candidate_needs_history(tmp_path):
    db = _db(tmp_path)
    for i, amt in enumerate([2000, 2000, 2000, 2050, 6000]):
        _tx(db, f"p{i}", "hysa", f"2026-0{i + 3}-15", -amt, "ACME CORP PAYROLL", "INCOME", "INCOME_OTHER")
    rows = classify(db.conn, parse_config(CFG))
    assert [r.transaction_id for r in rows if r.bonus_candidate] == ["p4"]
    s = summarize(rows)
    assert s["classes"]["paycheck"] == 5 and s["bonus_candidates"] == 1 and s["review"] == 0

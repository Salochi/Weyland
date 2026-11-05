from datetime import date

import pytest

from weyland.classify import WaterfallConfigError, classify, parse_config
from weyland.dashboard import rewards, spending

from test_classify import CFG, _db, _tx
from test_dashboard import TODAY

# Quicksilver-style flat card + Savor-style bonus card (typical issuer setups).
RW = {
    "rewards": {
        "card": [
            {"card": "Bank:3333", "base": 0.02, "balance": 100.0, "balance_as_of": "2026-08-01"},
            {"card": "CU:Rewards Card", "base": 0.01, "bonus": [
                {"label": "groceries", "rate": 0.03,
                 "plaid": ["FOOD_AND_DRINK_GROCERIES"], "merchants": ["(?i)^(ALDI|LIDL)"]}]},
        ]
    }
}


def rw_cfg(**over):
    rewards = over.pop("rewards", {})
    return parse_config({**CFG, "rewards": {**RW["rewards"], **rewards}, **over})


def test_rewards_config_validation():
    c = rw_cfg()
    assert c.rewards_cards[0]["base"] == 0.02 and c.rewards_cards[0]["balance"] == 100.0
    assert c.rewards_cards[1]["bonus"][0]["plaid"] == ("FOOD_AND_DRINK_GROCERIES",)
    assert c.redemption_patterns  # default patterns exist
    with pytest.raises(WaterfallConfigError, match="Institution:mask-or-name"):
        rw_cfg(rewards={"card": [{"card": "no-colon", "base": 0.02}]})
    with pytest.raises(WaterfallConfigError, match="fractions"):
        rw_cfg(rewards={"card": [{"card": "Bank:3333", "base": 3}]})
    with pytest.raises(WaterfallConfigError, match="balance_as_of"):
        rw_cfg(rewards={"card": [{"card": "Bank:3333", "base": 0.02, "balance": 5}]})


def test_rewards_estimates_bonus_and_base(tmp_path):
    db = _db(tmp_path)
    _tx(db, "n1", "cc", "2026-09-10", 200, "SHELL OIL", "TRANSPORTATION", "TRANSPORTATION_GAS")
    _tx(db, "n2", "cucc", "2026-09-12", 100, "ALDI", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    _tx(db, "n3", "cucc", "2026-09-13", 50, "HARRIS TEETER", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    _tx(db, "n4", "cucc", "2026-09-14", 80, "STEAM GAMES", "ENTERTAINMENT", "ENTERTAINMENT_VIDEO_GAMES")
    # merchant regex hits the bonus, but Plaid's detail says general merchandise
    _tx(db, "n5", "cucc", "2026-09-15", 40, "LIDL STORE 22", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES")
    # fully refunded purchase earns nothing
    _tx(db, "r1", "cucc", "2026-09-20", 30, "ALDI", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    _tx(db, "r1back", "cucc", "2026-09-25", -30, "ALDI", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    d = rewards(db.conn, rw_cfg(), "2026-09-01", "2026-09-30", today=TODAY)
    flat = next(c for c in d["cards"] if c["card"] == "Bank:3333")
    bonus = next(c for c in d["cards"] if c["card"] == "CU:Rewards Card")
    assert flat["estimated"] == 4.0          # 200 * 2%
    assert bonus["purchases"] == 4           # n2, n3, n4, n5 kept; the refunded r1 excluded
    # 100*3% (merchant) + 50*3% (plaid) + 40*3% (merchant-only, non-grocery Plaid detail) + 80*1%
    assert bonus["estimated"] == round(3 + 1.5 + 1.2 + 0.8, 2)
    assert bonus["per_month"] == {"2026-09": 6.5}
    assert d["estimated_total"] == round(flat["estimated"] + bonus["estimated"], 2)


def test_rewards_redemption_detection_and_projection(tmp_path):
    db = _db(tmp_path)
    _tx(db, "buy", "cc", "2026-09-05", 100, "SHELL OIL", "TRANSPORTATION", "TRANSPORTATION_GAS")
    # earned before the balance date: counted in the span estimate, excluded from the projection
    _tx(db, "old", "cc", "2026-07-20", 300, "SHELL OIL 2", "TRANSPORTATION", "TRANSPORTATION_GAS")
    # statement credit on the card and a rewards deposit into the bank
    _tx(db, "credit", "cc", "2026-09-20", -4, "CAPITAL ONE CASH BACK REWARD", "", "")
    _tx(db, "dep", "hysa", "2026-09-22", -25, "NFCU CASH REWARDS REDEMPTION", "TRANSFER_IN", "")
    d = rewards(db.conn, rw_cfg(), "2026-09-01", "2026-09-30", today=TODAY)
    rows = {r.transaction_id: r for r in classify(db.conn, rw_cfg(), today=TODAY)}
    assert rows["credit"].cls == "other_income" and rows["dep"].cls == "other_income"
    flat = next(c for c in d["cards"] if c["card"] == "Bank:3333")
    # balance 100 (as of Aug 1) + 2%*100 earned since − 4 statement credit redeemed since
    assert flat["redeemed"] == 4.0
    # July's 6 sits outside the Sep span (not in `estimated`) and before the balance date
    # (not in the projection): earned_after = 100 balance + 2 since Aug 1 − 4 redeemed
    assert flat["estimated"] == 2.0
    assert flat["earned_after"] == round(100 + 2 - 4, 2)
    assert d["redeemed_deposits"] == 25.0 and d["redeemed_total"] == 29.0


def test_spending_rows_carry_cashback(tmp_path):
    db = _db(tmp_path)
    # a Savor grocery (bonus), a Quicksilver purchase (base), a bank debit, a refund pair
    _tx(db, "sv", "cucc", "2026-09-12", 100, "ALDI", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    _tx(db, "qk", "cc", "2026-09-13", 80, "AMZN MKTP", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACE")
    _tx(db, "dbt", "chk", "2026-09-14", 50, "SHELL OIL DEBIT", "TRANSPORTATION", "TRANSPORTATION_GAS")
    _tx(db, "rf", "cucc", "2026-09-20", 30, "LIDL", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    _tx(db, "rfback", "cucc", "2026-09-25", -30, "LIDL", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    c = rw_cfg()
    d = spending(db.conn, c, "2026-09-01", "2026-09-30", buckets=("spending",), today=TODAY)
    rows = {r["transaction_id"]: r for r in d["rows"]}
    assert rows["sv"]["cashback"] == {"rate": 0.03, "label": "groceries", "cashback": 3.0}
    assert rows["qk"]["cashback"] == {"rate": 0.02, "label": "", "cashback": 1.6}
    assert rows["dbt"]["cashback"] is None          # not a card purchase
    assert rows["rf"]["cashback"] is None          # refunded pair earns nothing
    assert rows["rfback"]["cashback"] is None


def test_rewards_no_config_is_empty(tmp_path):
    db = _db(tmp_path)
    d = rewards(db.conn, parse_config(CFG), "2026-09-01", "2026-09-30", today=TODAY)
    d = rewards(db.conn, parse_config(CFG), "2026-09-01", "2026-09-30", today=TODAY)
    assert d["cards"] == [] and d["estimated_total"] == 0.0 and d["redeemed_total"] == 0.0
    # spending rows carry no cashback without [rewards]
    s = spending(db.conn, parse_config(CFG), "2026-09-01", "2026-09-30", buckets=("spending",), today=TODAY)
    assert all(r["cashback"] is None for r in s["rows"])


def test_empty_redemptions_keeps_the_default_patterns(tmp_path):
    # The example config ships `redemptions = []`; that must add nothing, not
    # switch off cashback detection (the credit would fall through to `refund`).
    c = rw_cfg(rewards={"redemptions": []})
    assert c.redemption_patterns[:2] == (r"\b(rewards?|cash ?back)\b", r"\bredemption\b")
    extra = rw_cfg(rewards={"redemptions": ["(?i)^POINTS PAYOUT"]})
    assert "(?i)^POINTS PAYOUT" in extra.redemption_patterns and r"\bredemption\b" in extra.redemption_patterns
    db = _db(tmp_path)
    _tx(db, "credit", "cc", "2026-09-20", -4, "CASH BACK REWARD", "", "")
    rows = {r.transaction_id: r for r in classify(db.conn, c, today=TODAY)}
    assert rows["credit"].cls == "other_income"


def test_projection_counts_purchases_before_the_displayed_span(tmp_path):
    # balance_as_of (Aug 1) is older than the requested span (Sep): the projection
    # must still add what was earned in August, not only what the span shows.
    db = _db(tmp_path)
    _tx(db, "aug", "cc", "2026-08-10", 300, "SHELL OIL 1", "TRANSPORTATION", "TRANSPORTATION_GAS")
    _tx(db, "sep", "cc", "2026-09-05", 100, "SHELL OIL 2", "TRANSPORTATION", "TRANSPORTATION_GAS")
    _tx(db, "oct", "cc", "2026-10-05", 50, "SHELL OIL 3", "TRANSPORTATION", "TRANSPORTATION_GAS")
    d = rewards(db.conn, rw_cfg(), "2026-09-01", "2026-09-30", today=TODAY)
    flat = next(c for c in d["cards"] if c["card"] == "Bank:3333")
    assert flat["estimated"] == 2.0                    # the span: September only
    assert flat["earned_after"] == round(100 + 6 + 2, 2)  # Aug + Sep since Aug 1, nothing after the span end
    # the UI's formula line shows these parts, so they must add up to the headline
    assert flat["earned_since"] == 8.0 and flat["redeemed_since"] == 0.0
    assert flat["earned_after"] == round(flat["balance"] + flat["earned_since"] - flat["redeemed_since"], 2)

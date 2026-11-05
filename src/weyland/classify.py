"""Transaction classification for the monthly close.

Every posted bank/card transaction in an in-scope account gets exactly one
class plus a human-readable reason. Rules come from ``waterfall.toml`` (the user's
names and patterns never live in the repo); manual overrides in
``txn_overrides`` always win. Anything ambiguous lands in ``unknown`` (the
review queue) rather than being guessed into income or spending.

Plaid sign convention: amount > 0 is money leaving the account, < 0 is money
arriving (for cards: < 0 is a payment or refund).
"""

from __future__ import annotations

import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CLASSES = (
    "paycheck", "interest", "other_income", "spending", "refund", "reimbursement",
    "internal_transfer", "card_payment", "investment_contribution", "unknown",
)
ROLES = ("hysa", "checking", "card", "brokerage", "roth", "retirement", "ignore")
# Roles whose bank-style transactions feed the close.
CASH_ROLES = ("hysa", "checking", "card")


class WaterfallConfigError(RuntimeError):
    pass


# Rent and recurring bills: required monthly costs, kept apart from spending.
DEFAULT_FIXED = ("rent", "rent_and_utilities", "insurance")
BUCKETS = ("spending", "subscriptions", "fixed")


@dataclass(frozen=True)
class WaterfallConfig:
    accounts: dict[str, list[str]]          # role -> selectors "Institution:mask-or-name"
    employer: tuple[str, ...]               # paycheck sender patterns
    reimbursers: tuple[str, ...]            # people who pay the user back (roommate)
    card_payment: tuple[str, ...]           # extra outflow patterns that pay a card
    internal: tuple[str, ...]               # own-account transfer patterns
    investment: tuple[str, ...]             # outflows that fund Roth/brokerage
    hysa_floor: float = 20000.0
    bonus_multiple: float = 1.5
    match_window_days: int = 5
    roth_limits: dict[int, float] = field(default_factory=dict)
    categories: dict[str, tuple[str, ...]] = field(default_factory=dict)  # label -> name patterns
    close_grace_days: int = 3
    start: str | None = None                # first YYYY-MM close that gets a recommendation
    earmarks: tuple[dict, ...] = ()         # {name, amount, status open|released, due, note}
    fixed: tuple[str, ...] = DEFAULT_FIXED  # category labels counted as fixed costs, not spending
    subscriptions: tuple[str, ...] = ("subscriptions",)  # category labels shown as subscriptions
    card_limits: dict[str, float] = field(default_factory=dict)  # selector -> limit (when Plaid has none)
    manual_pay: tuple[str, ...] = ()        # card selectors with no autopay (flag their due dates)
    rewards_cards: tuple[dict, ...] = ()    # {card, base, bonus: ({label, rate, plaid, merchants}), balance, balance_as_of}
    redemption_patterns: tuple[str, ...] = ()  # names of cashback deposits / statement credits

    @property
    def open_earmarks(self) -> list[dict]:
        return [e for e in self.earmarks if e["status"] == "open"]


DEFAULT_INTERNAL = (r"^Deposit from (Savings|Checking)\b", r"^Withdrawal to (Savings|Checking)\b")
# Cashback paid out as a statement credit or a deposit (inflows only).
DEFAULT_REDEMPTIONS = (r"\b(rewards?|cash ?back)\b", r"\bredemption\b")


def _parse_rewards(raw) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise WaterfallConfigError("rewards must be a table")
    # rewards.redemptions *adds to* the defaults: an empty list must not switch
    # off "cash back"/"rewards" detection (the example config ships it empty).
    pats = DEFAULT_REDEMPTIONS + tuple(p for p in _patterns(raw, "redemptions") if p not in DEFAULT_REDEMPTIONS)
    cards = []
    for i, c in enumerate(raw.get("card", [])):
        sel = c.get("card") if isinstance(c, dict) else None
        if not isinstance(sel, str) or ":" not in sel:
            raise WaterfallConfigError(f"rewards.card #{i + 1} needs card = 'Institution:mask-or-name'")
        try:
            base = float(c.get("base", 0))
            bonus = tuple({"label": str(b.get("label", "")), "rate": float(b["rate"]),
                           "plaid": tuple(str(p).upper() for p in b.get("plaid", [])),
                           "merchants": _patterns(b, "merchants")} for b in c.get("bonus", []))
            balance = None if c.get("balance") is None else float(c["balance"])
        except (KeyError, TypeError, ValueError, AttributeError) as err:
            raise WaterfallConfigError(f"rewards.card {sel!r}: base/bonus rates/balance must be numbers") from err
        if not 0 <= base < 0.2 or any(not 0 <= b["rate"] < 0.2 for b in bonus):
            raise WaterfallConfigError(f"rewards.card {sel!r}: rates are fractions (0.03 = 3%)")
        as_of = c.get("balance_as_of")
        if balance is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(as_of or "")):
            raise WaterfallConfigError(f"rewards.card {sel!r}: balance needs balance_as_of = YYYY-MM-DD")
        cards.append({"card": sel, "base": base, "bonus": bonus, "balance": balance,
                      "balance_as_of": str(as_of) if balance is not None else None})
    return tuple(cards), pats


def _patterns(raw: dict, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    vals = raw.get(key, list(default))
    if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
        raise WaterfallConfigError(f"rules.{key} must be a list of strings")
    for v in vals:
        try:
            re.compile(v)
        except re.error as e:
            raise WaterfallConfigError(f"rules.{key}: bad regex {v!r}: {e}") from e
    return tuple(vals)


def parse_config(data: dict) -> WaterfallConfig:
    accts = data.get("accounts", {})
    bad = set(accts) - set(ROLES)
    if bad:
        raise WaterfallConfigError(f"unknown account roles: {sorted(bad)}")
    accounts = {role: ([v] if isinstance(v, str) else list(v)) for role, v in accts.items()}
    if len(accounts.get("hysa", [])) != 1:
        raise WaterfallConfigError("accounts.hysa must name exactly one account")
    rules = data.get("rules", {})
    wf = data.get("waterfall", {})
    limits = {int(y): float(v) for y, v in data.get("roth_limits", {}).items()}
    cats_raw = data.get("categories", {})
    if not isinstance(cats_raw, dict):
        raise WaterfallConfigError("categories must be a table of label = [patterns]")
    categories = {label: _patterns(cats_raw, label) for label in cats_raw}
    start = wf.get("start")
    if start is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(start)):
        raise WaterfallConfigError("waterfall.start must be YYYY-MM")
    earmarks = []
    for i, e in enumerate(data.get("earmark", [])):
        if not isinstance(e, dict) or not e.get("name"):
            raise WaterfallConfigError(f"earmark #{i + 1} needs a name")
        try:
            amount = float(e["amount"])
        except (KeyError, TypeError, ValueError) as err:
            raise WaterfallConfigError(f"earmark {e['name']!r} needs a numeric amount") from err
        status = e.get("status", "open")
        if status not in ("open", "released"):
            raise WaterfallConfigError(f"earmark {e['name']!r}: status must be open or released")
        earmarks.append({"name": str(e["name"]), "amount": amount, "status": status,
                         "due": str(e.get("due", "")), "note": str(e.get("note", ""))})
    sp = data.get("spending", {})
    labels = {}
    for key, default in (("fixed", DEFAULT_FIXED), ("subscriptions", ("subscriptions",))):
        v = sp.get(key, list(default))
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise WaterfallConfigError(f"spending.{key} must be a list of category labels")
        labels[key] = tuple(x.lower() for x in v)
    if set(labels["fixed"]) & set(labels["subscriptions"]):
        raise WaterfallConfigError("a category can't be both fixed and a subscription")
    cards = data.get("cards", {})
    try:
        card_lim = {str(k): float(v) for k, v in cards.get("limits", {}).items()}
    except (TypeError, ValueError, AttributeError) as err:
        raise WaterfallConfigError("cards.limits must map 'Institution:mask' = amount") from err
    for k in card_lim:
        if ":" not in k:
            raise WaterfallConfigError(f"cards.limits key {k!r} must be 'Institution:mask-or-name'")
    manual = cards.get("manual_pay", [])
    if not isinstance(manual, list) or not all(isinstance(x, str) and ":" in x for x in manual):
        raise WaterfallConfigError("cards.manual_pay must be a list of 'Institution:mask-or-name'")
    rewards_cards, redemption_patterns = _parse_rewards(data.get("rewards", {}))
    return WaterfallConfig(
        rewards_cards=rewards_cards,
        redemption_patterns=redemption_patterns,
        accounts=accounts,
        employer=_patterns(rules, "employer"),
        reimbursers=_patterns(rules, "reimbursers"),
        card_payment=_patterns(rules, "card_payment"),
        internal=_patterns(rules, "internal", DEFAULT_INTERNAL),
        investment=_patterns(rules, "investment"),
        hysa_floor=float(wf.get("hysa_floor", 20000)),
        bonus_multiple=float(wf.get("bonus_multiple", 1.5)),
        match_window_days=int(wf.get("match_window_days", 5)),
        roth_limits=limits,
        categories=categories,
        close_grace_days=int(wf.get("close_grace_days", 3)),
        start=str(start) if start else None,
        earmarks=tuple(earmarks),
        fixed=labels["fixed"],
        subscriptions=labels["subscriptions"],
        card_limits=card_lim,
        manual_pay=tuple(manual),
    )


def load_config(path: Path | None = None) -> WaterfallConfig:
    if path is None:
        path = Path(os.environ.get("WEYLAND_CONFIG_DIR", "/etc/weyland")) / "waterfall.toml"
    if not path.exists():
        raise WaterfallConfigError(f"missing {path} (see deploy/waterfall.toml.example)")
    with path.open("rb") as f:
        return parse_config(tomllib.load(f))


def resolve_roles(conn: sqlite3.Connection, cfg: WaterfallConfig) -> dict[str, str]:
    """Map account_id -> role. A selector must match exactly one account."""
    rows = conn.execute(
        "SELECT a.account_id, i.institution_name, a.mask, a.name FROM accounts a "
        "JOIN items i ON i.item_id = a.item_id").fetchall()
    roles: dict[str, str] = {}
    for role, selectors in cfg.accounts.items():
        for sel in selectors:
            # rpartition: institution names may themselves contain ':' (e.g.
            # "First Platypus Bank (sandbox: transactions)"); the mask/name
            # never does, so the LAST colon is the separator.
            inst, sep, key = sel.rpartition(":")
            if not sep:
                raise WaterfallConfigError(f"selector {sel!r} must be 'Institution:mask-or-name'")
            hits = [r[0] for r in rows if r[1] == inst and key in (r[2], r[3])]
            if len(hits) != 1:
                raise WaterfallConfigError(f"selector {sel!r} matched {len(hits)} accounts (need 1)")
            if hits[0] in roles:
                raise WaterfallConfigError(f"account {sel!r} has two roles")
            roles[hits[0]] = role
    return roles


def select_account(conn: sqlite3.Connection, sel: str) -> str:
    """account_id for one 'Institution:mask-or-name' selector (must match exactly one)."""
    inst, _, key = sel.rpartition(":")  # last colon: institution names may contain ':'
    hits = [r[0] for r in conn.execute(
        "SELECT a.account_id FROM accounts a JOIN items i ON i.item_id = a.item_id "
        "WHERE i.institution_name = ? AND (a.mask = ? OR a.name = ?)", (inst, key, key))]
    if len(hits) != 1:
        raise WaterfallConfigError(f"selector {sel!r} matched {len(hits)} accounts (need 1)")
    return hits[0]


@dataclass
class Classified:
    transaction_id: str
    account_id: str
    role: str
    date: str
    amount: float
    name: str
    cls: str
    reason: str
    bonus_candidate: bool = False
    bonus_decision: str | None = None   # 'bonus' | 'paycheck' once the user decides a candidate
    overridden: bool = False
    match_status: str = ""          # matched | refunded | unmatched | awaiting | outside_history | confirmed
    match_with: str | None = None
    category: str = ""
    bucket: str = ""                # spending | subscriptions | fixed (purchases and refunds only)
    merchant: str = ""
    plaid_detail: str = ""          # Plaid's detailed category (card reward categories key off it)


def category_for(t: dict, cfg: WaterfallConfig) -> str:
    """Spending category: a configured pattern (on name or merchant) wins, else Plaid's."""
    texts = [s for s in (t["name"], t["merchant_name"]) if s]
    for label, pats in cfg.categories.items():
        if any(_match(pats, s) for s in texts):
            return label
    if t.get("category_detail") == "GENERAL_SERVICES_INSURANCE":
        return "insurance"
    return (t["category_primary"] or "uncategorised").lower()


def bucket_for(cls: str, category: str, cfg: WaterfallConfig) -> str:
    if cls not in ("spending", "refund"):
        return ""
    if category in cfg.fixed:
        return "fixed"
    return "subscriptions" if category in cfg.subscriptions else "spending"


def _match(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


_CARD_PAYMENT_NAME = re.compile(r"\b(PAYMENT|PYMT)\b", re.IGNORECASE)
_INTEREST = {"INCOME_INTEREST_EARNED", "INCOME_DIVIDENDS"}


def classify_one(t: dict, role: str, cfg: WaterfallConfig) -> tuple[str, str]:
    amt, name = t["amount"], (t["name"] or t["merchant_name"] or "")
    prim, det = t["category_primary"] or "", t["category_detail"] or ""
    inflow = amt < 0

    if inflow and _match(cfg.redemption_patterns, name):
        return "other_income", "cashback redemption (rewards.redemptions)"
    if role == "card":
        if inflow:
            if prim in ("LOAN_PAYMENTS", "TRANSFER_IN") or _CARD_PAYMENT_NAME.search(name):
                return "card_payment", f"card inflow ({det or 'payment'})"
            return "refund", "card credit from a merchant"
        return "spending", "card purchase"

    # depository: hysa / checking
    if inflow and _match(cfg.reimbursers, name):
        return "reimbursement", "repayment from rules.reimbursers (offsets spending)"
    if inflow and _match(cfg.employer, name):
        return "paycheck", "employer deposit (rules.employer)"
    if inflow and det in _INTEREST:
        return "interest", det
    if _match(cfg.internal, name):
        return "internal_transfer", "own-account transfer pattern"
    if not inflow and (det == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT" or _match(cfg.card_payment, name)):
        return "card_payment", "pays one of your cards"
    if not inflow and _match(cfg.investment, name):
        return "investment_contribution", "funds Roth/brokerage (rules.investment)"
    if prim in ("TRANSFER_IN", "TRANSFER_OUT", "OTHER", "INCOME") or not prim:
        if not inflow and det == "TRANSFER_OUT_WITHDRAWAL":
            return "spending", "cash withdrawal"
        return "unknown", f"unrecognised {det or prim or 'uncategorised'}; needs review"
    if inflow:
        return "refund", f"inflow categorised {prim}"
    return "spending", prim


def classify(conn: sqlite3.Connection, cfg: WaterfallConfig,
             month: str | None = None, today=None) -> list[Classified]:
    """Classify + match posted transactions in cash-role accounts (optionally one YYYY-MM).

    Matching always runs over the full history so pairs straddling a month
    boundary are found; the month filter is applied afterwards.
    """
    from .match import match
    roles = resolve_roles(conn, cfg)
    conn.row_factory = sqlite3.Row
    overrides = {}
    if _has_table(conn, "txn_overrides"):
        has_cat = "category" in {r[1] for r in conn.execute("PRAGMA table_info(txn_overrides)")}
        overrides = {r["transaction_id"]: (r["class"], r["note"], r["category"] if has_cat else None)
                     for r in conn.execute("SELECT * FROM txn_overrides")}
    out: list[Classified] = []
    for t in conn.execute("SELECT * FROM transactions WHERE pending = 0 ORDER BY date, transaction_id"):
        role = roles.get(t["account_id"])
        if role not in CASH_ROLES:
            continue  # ignored, investment, or unassigned accounts
        cat_override = None
        if t["transaction_id"] in overrides:
            cls, note, cat_override = overrides[t["transaction_id"]]
            reason = f"manual override{': ' + note if note else ''}"
        else:
            cls, reason = classify_one(dict(t), role, cfg)
        cat = cat_override or category_for(dict(t), cfg)
        out.append(Classified(t["transaction_id"], t["account_id"], role, t["date"], t["amount"],
                              t["name"] or "", cls, reason, overridden=t["transaction_id"] in overrides,
                              category=cat, bucket=bucket_for(cls, cat, cfg),
                              merchant=t["merchant_name"] or t["name"] or "",
                              plaid_detail=(t["category_detail"] or "").upper()))
    _flag_bonus_candidates(out, cfg)
    if _has_table(conn, "bonus_decisions"):
        dec = dict(conn.execute("SELECT transaction_id, decision FROM bonus_decisions").fetchall())
        for r in out:
            if r.bonus_candidate:
                r.bonus_decision = dec.get(r.transaction_id)
    match(out, cfg.match_window_days, today)
    return [r for r in out if r.date[:7] == month] if month else out


def _flag_bonus_candidates(rows: list[Classified], cfg: WaterfallConfig) -> None:
    pays = sorted(-r.amount for r in rows if r.cls == "paycheck")
    if len(pays) < 3:
        return  # not enough history for a meaningful median
    median = pays[len(pays) // 2]
    for r in rows:
        if r.cls == "paycheck" and -r.amount > cfg.bonus_multiple * median:
            r.bonus_candidate = True


def set_override(conn: sqlite3.Connection, transaction_id: str, cls: str | None, note: str = "",
                 category: str | None = None) -> None:
    """Pin a transaction's class, optionally its spending category (cls=None clears)."""
    from .db import now_iso
    if conn.execute("SELECT 1 FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone() is None:
        raise WaterfallConfigError(f"no such transaction {transaction_id!r}")
    if cls is None:
        conn.execute("DELETE FROM txn_overrides WHERE transaction_id=?", (transaction_id,))
    else:
        if cls not in CLASSES or cls == "unknown":
            raise WaterfallConfigError(f"class must be one of {[c for c in CLASSES if c != 'unknown']}")
        conn.execute("INSERT INTO txn_overrides(transaction_id, class, note, category, at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(transaction_id) DO UPDATE SET class=excluded.class, note=excluded.note, "
                     "category=excluded.category, at=excluded.at",
                     (transaction_id, cls, note, category or None, now_iso()))
    conn.commit()


def unassigned_accounts(conn: sqlite3.Connection, cfg: WaterfallConfig) -> list[str]:
    roles = resolve_roles(conn, cfg)
    return [f"{r[1]}:{r[2] or r[3]}" for r in conn.execute(
        "SELECT a.account_id, i.institution_name, a.mask, a.name FROM accounts a "
        "JOIN items i ON i.item_id = a.item_id") if r[0] not in roles]


def summarize(rows: list[Classified]) -> dict:
    from .match import needs_review, summarize as match_summary
    counts = {c: 0 for c in CLASSES}
    for r in rows:
        counts[r.cls] += 1
    return {"total": len(rows), "classes": counts, "matching": match_summary(rows),
            "review": sum(needs_review(r) for r in rows),
            "bonus_candidates": sum(r.bonus_candidate for r in rows)}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

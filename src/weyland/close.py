"""Monthly close: income, spending and surplus per calendar month.

Figures (all positive numbers, dollars):

- ``paychecks``     regular employer deposits (bonus candidates excluded)
- ``bonus_candidates`` deposits flagged as possible bonus that the user hasn't
  decided yet (a close is blocked until he does); never part of the surplus
- ``bonus``         candidates the user confirmed as bonus; routed by the waterfall,
  never part of the surplus. A candidate he marks "paycheck" counts as one.
- ``spending``      what the user bought: purchases − unpaired refunds, excluding
  fixed costs and subscriptions. Fully refunded purchases and their refunds
  are dropped entirely (they net to 0). Card payments and internal transfers
  never count.
- ``subscriptions`` recurring services (categories in ``[spending]
  subscriptions``), net of refunds.
- ``fixed``         rent and recurring bills (``[spending] fixed``) minus
  reimbursements: the roommate's Zelle-ins repay his share of rent/utilities.
- ``outflows``      spending + subscriptions + fixed (what v1 called spending;
  closes frozen before the split are drift-checked against this).
- ``surplus``       paychecks − outflows. A health metric only: the waterfall
  works off the HYSA balance, so this is never added on top of it.
- ``interest`` and ``other_income`` (gifts, rewards payouts) are shown
  separately and stay out of the surplus.

A month can be closed once it is over plus ``close_grace_days`` (Capital One
posts late), every in-scope institution's history covers the whole month, and
nothing in it is waiting for review or for a transfer's other leg. Closing freezes the figures in ``month_closes``; recomputing later and
getting different numbers is reported as drift instead of silently changing
history.

Month-end HYSA balance: Plaid only reports the balance "now", so the balance
at the end of month M is reconstructed by replaying posted transactions
backwards from the latest reported balance:

    balance(end of M) = current + sum(amount of posted txns dated after M)

(Plaid sign: amount > 0 is money out, so adding it back undoes it.) This is
exact as long as no posted transaction is missing. Daily balance snapshots,
recorded on every sync, check it: a snapshot taken on day d must match the
reconstruction for d, d-1 or d-2 within $1, otherwise the month is flagged.
The sync may run before or after that day's postings, and banks can lag Plaid's
posting date by up to two days over a weekend (Oct 31 2026 was a Saturday).
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from .classify import WaterfallConfig, classify, resolve_roles
from .db import now_iso
from .match import needs_review
from .waterfall import recommend

FIELDS = ("paychecks", "bonus_candidates", "bonus", "spending", "subscriptions", "fixed", "outflows",
          "surplus", "interest", "other_income", "purchases", "refunds", "reimbursements")
# Closes frozen before the spending/subscriptions/fixed split stored the total as "spending".
LEGACY = {"outflows": "spending"}


class CloseError(RuntimeError):
    pass


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


BALANCE_TOLERANCE = 1.00
# A snapshot may lag the posting dates by this many days (weekend postings).
BALANCE_LAG_DAYS = 2


def _hysa_id(conn: sqlite3.Connection, cfg: WaterfallConfig) -> str | None:
    return next((a for a, r in resolve_roles(conn, cfg).items() if r == "hysa"), None)


class BalanceReplay:
    """Reconstruct an account's end-of-day balance on past dates.

    Depository accounts: balance = current + later amounts (amount > 0 left).
    Credit cards (``credit=True``): Plaid's ``current`` is the amount owed and
    a purchase (amount > 0) raised it, so owed = current − later amounts.
    """

    def __init__(self, conn: sqlite3.Connection, account_id: str, credit: bool = False):
        self.sign = -1 if credit else 1
        row = conn.execute("SELECT current FROM accounts WHERE account_id=?", (account_id,)).fetchone()
        self.current = row[0] if row else None
        self.txns = [(r[0], r[1]) for r in conn.execute(
            "SELECT date, amount FROM transactions WHERE account_id=? AND pending=0", (account_id,))]
        self.snaps = [(r[0], r[1]) for r in conn.execute(
            "SELECT date, current FROM balance_snapshots WHERE account_id=? AND current IS NOT NULL "
            "ORDER BY date", (account_id,))]

    def at(self, d: str) -> float | None:
        if self.current is None:
            return None
        return round(self.current + self.sign * sum(a for dt, a in self.txns if dt > d), 2)

    def mismatches(self, start: str, end: str) -> list[dict]:
        """Snapshots in [start, end] that don't match the reconstruction."""
        out = []
        for d, reported in self.snaps:
            if not start <= d <= end:
                continue
            day = date.fromisoformat(d)
            cands = [self.at((day - timedelta(days=k)).isoformat()) for k in range(BALANCE_LAG_DAYS + 1)]
            if all(c is None or abs(c - reported) > BALANCE_TOLERANCE for c in cands):
                out.append({"date": d, "reported": reported, "reconstructed": cands[0]})
        return out


def figures(rows: list) -> dict:
    """Close figures for one month's classified rows."""
    s = {k: 0.0 for k in FIELDS}
    cats: dict[str, float] = {}
    by_bucket = {"spending": 0.0, "subscriptions": 0.0, "fixed": 0.0}
    for r in rows:
        if r.cls in ("spending", "refund") and r.match_status != "refunded":
            by_bucket[r.bucket or "spending"] += r.amount
        amt = r.amount  # Plaid: > 0 = money out
        if r.cls == "paycheck":
            if not r.bonus_candidate or r.bonus_decision == "paycheck":
                s["paychecks"] += -amt
            else:
                s["bonus" if r.bonus_decision == "bonus" else "bonus_candidates"] += -amt
        elif r.cls == "interest":
            s["interest"] += -amt
        elif r.cls == "other_income":
            s["other_income"] += -amt
        elif r.cls in ("spending", "refund") and r.match_status == "refunded":
            continue  # full refund pair nets to zero
        elif r.cls == "spending":
            s["purchases"] += amt
            cats[r.category] = cats.get(r.category, 0.0) + amt
        elif r.cls == "refund":
            s["refunds"] += -amt
            cats[r.category] = cats.get(r.category, 0.0) + amt
        elif r.cls == "reimbursement":
            s["reimbursements"] += -amt
            cats["reimbursed"] = cats.get("reimbursed", 0.0) + amt
    s["spending"] = by_bucket["spending"]
    s["subscriptions"] = by_bucket["subscriptions"]
    s["fixed"] = by_bucket["fixed"] - s["reimbursements"]
    s["outflows"] = s["spending"] + s["subscriptions"] + s["fixed"]
    s["surplus"] = s["paychecks"] - s["outflows"]
    out: dict[str, Any] = {k: round(v, 2) for k, v in s.items()}
    out["categories"] = {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])}
    return out


def months(conn: sqlite3.Connection, cfg: WaterfallConfig, today: date | None = None) -> list[dict]:
    """Every month with data, newest first, with figures and close status."""
    today = today or date.today()
    rows = classify(conn, cfg, today=today)
    if not rows:
        return []
    item_of: dict[str, str] = {r[0]: r[1] for r in conn.execute("SELECT account_id, item_id FROM accounts")}
    roles = resolve_roles(conn, cfg)
    item_start: dict[str, str] = {}
    for r in rows:
        it = item_of[r.account_id]
        item_start[it] = min(item_start.get(it, r.date), r.date)
    cash_items = {item_of[a] for a, role in roles.items() if role in ("hysa", "checking", "card")}
    snaps = {r["month"]: r for r in conn.execute("SELECT * FROM month_closes")}
    hysa = _hysa_id(conn, cfg)
    replay = BalanceReplay(conn, hysa) if hysa else None
    card_names = {r[0]: f"{r[1]} {r[2]}" for r in conn.execute(
        "SELECT a.account_id, i.institution_name, COALESCE(a.mask, a.name) FROM accounts a "
        "JOIN items i USING(item_id)")}
    cards = {a: BalanceReplay(conn, a, credit=True) for a, role in roles.items() if role == "card"}

    out = []
    for m in sorted({r.date[:7] for r in rows} | set(snaps), reverse=True):
        start, end = _month_bounds(m)
        in_m = [r for r in rows if r.date[:7] == m]
        fig = figures(in_m)
        partial = sorted(it for it in cash_items if item_start.get(it, "9999") > start.isoformat())
        blockers = []
        if today <= end + timedelta(days=cfg.close_grace_days):
            blockers.append("month not over" if today <= end else
                            f"waiting {cfg.close_grace_days}-day grace for late postings")
        if partial:
            blockers.append("partial history (an institution's data starts mid-month or later)")
        n_review = sum(needs_review(r) for r in in_m)
        if n_review:
            blockers.append(f"{n_review} transaction(s) need review")
        n_wait = sum(r.match_status == "awaiting" for r in in_m)
        if n_wait:
            blockers.append(f"{n_wait} transfer(s) awaiting the other side")
        bal = None
        if replay and today > end:
            check = replay.mismatches(start.isoformat(), (end + timedelta(days=1)).isoformat())
            bal = {"date": end.isoformat(), "amount": replay.at(end.isoformat()), "mismatches": check}
            if check:
                blockers.append("HYSA balance check failed: reported balance doesn't match transactions")
        owed = None
        if today > end and cards:
            owed, bad = {}, []
            for a, rp in cards.items():
                owed[card_names.get(a, a)] = rp.at(end.isoformat()) or 0.0
                if rp.mismatches(start.isoformat(), (end + timedelta(days=1)).isoformat()):
                    bad.append(card_names.get(a, a))
            if bad:
                blockers.append("card balance check failed: " + ", ".join(sorted(bad)))
        n_undecided = sum(r.bonus_candidate and r.bonus_decision is None for r in in_m)
        if n_undecided:
            blockers.append(f"{n_undecided} possible bonus deposit(s) need a decision")
        entry = {"month": m, "figures": fig, "partial_history": bool(partial),
                 "partial_institutions": partial, "blockers": blockers,
                 "status": "closed" if m in snaps else ("closeable" if not blockers else "open"),
                 "month_end_balance": bal, "cards_owed": owed,
                 "bonus_candidates": [{"transaction_id": r.transaction_id, "date": r.date, "amount": -r.amount,
                                       "decision": r.bonus_decision} for r in in_m if r.bonus_candidate],
                 "recommendation": None}
        if cfg.start and m >= cfg.start and bal and owed is not None and m not in snaps:
            entry["recommendation"] = recommend(conn, cfg, m, bal["amount"], owed, fig["bonus"])
        if m in snaps:
            snap = snaps[m]
            frozen = json.loads(snap["figures_json"])
            entry["closed_at"] = snap["closed_at"]
            entry["frozen"] = frozen
            entry["hysa_balance"] = snap["hysa_balance"]
            entry["balance_as_of"] = snap["balance_as_of"]
            if snap["recommendation_json"]:
                entry["recommendation"] = json.loads(snap["recommendation_json"])
            legacy = "fixed" not in frozen
            entry["drift"] = {}
            for k in FIELDS:
                fk = LEGACY.get(k, k) if legacy else k
                if legacy and (k in ("spending", "subscriptions", "fixed") or fk not in frozen):
                    continue  # not comparable with a pre-split snapshot
                if abs((frozen.get(fk) or 0) - fig[k]) >= 0.005:
                    entry["drift"][k] = {"frozen": frozen.get(fk), "now": fig[k]}
            now_bal = bal["amount"] if bal else None
            if snap["hysa_balance"] is not None and now_bal is not None \
                    and abs(snap["hysa_balance"] - now_bal) >= 0.005:
                entry["drift"]["hysa_balance"] = {"frozen": snap["hysa_balance"], "now": now_bal}
        out.append(entry)
    return out


def close_month(conn: sqlite3.Connection, cfg: WaterfallConfig, month: str,
                today: date | None = None) -> dict:
    today = today or date.today()
    entry = next((e for e in months(conn, cfg, today) if e["month"] == month), None)
    if entry is None:
        raise CloseError(f"no data for {month}")
    if entry["status"] == "closed":
        raise CloseError(f"{month} is already closed (reopen it first)")
    if entry["blockers"]:
        raise CloseError(f"{month} can't be closed yet: " + "; ".join(entry["blockers"]))
    rec = entry["recommendation"]
    if rec and rec.get("error"):
        raise CloseError(f"{month} can't be closed yet: {rec['error']}")
    mb = entry["month_end_balance"] or {}
    bal, as_of = mb.get("amount"), mb.get("date")
    conn.execute("INSERT INTO month_closes(month, figures_json, hysa_balance, balance_as_of, balance_source, "
                 "recommendation_json, closed_at) VALUES (?,?,?,?,?,?,?)",
                 (month, json.dumps(entry["figures"]), bal, as_of,
                  "reconstructed" if bal is not None else None,
                  json.dumps(rec) if rec else None, now_iso()))
    conn.commit()
    return {"month": month, "hysa_balance": bal, "balance_as_of": as_of, "recommendation": rec}


def reopen_month(conn: sqlite3.Connection, month: str) -> None:
    if conn.execute("DELETE FROM month_closes WHERE month=?", (month,)).rowcount == 0:
        raise CloseError(f"{month} is not closed")
    conn.commit()

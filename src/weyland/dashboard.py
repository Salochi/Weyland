"""Read models for the v2 dashboard (overview cards + spending explorer).

Read-only views over the synced data. Sign conventions in everything returned
here are the human ones: money in / assets are positive, money out / debt is
negative, except where a field name says otherwise (``owed``, ``spent``).
"""

from __future__ import annotations

import json
import sqlite3
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

from .classify import (WaterfallConfig, WaterfallConfigError, _match, classify, resolve_roles,
                       select_account)
from .close import months as close_months
from .match import needs_review
from .waterfall import contributed, skipped_months, split_installments, tax_year, window_from

MANUAL_DUE_WARN_DAYS = 7
DEFAULT_REWARDS_SPAN = 120  # days of purchase history shown in the Rewards panel


def _cashback_rate(card_cfg: dict, r) -> tuple[float, str]:
    """Best matching bonus rate for a purchase, else the card's base rate."""
    texts = [s for s in (r.merchant, r.name) if s]
    for b in card_cfg["bonus"]:
        if b["plaid"] and r.plaid_detail in b["plaid"]:
            return b["rate"], b["label"]
        if any(_match(b["merchants"], s) for s in texts):
            return b["rate"], b["label"]
    return card_cfg["base"], ""



def rewards(conn: sqlite3.Connection, cfg: WaterfallConfig, start: str | None = None,
            end: str | None = None, today: date | None = None) -> dict:
    """Estimated cashback per card + actual redemptions + balances from config.

    Estimate: each counted card purchase earns the best matching bonus rate, else
    the card's base rate. Fully refunded purchases earn nothing (they net to zero).
    Redemptions: `other_income` rows matching rewards.redemptions — deposit redemptions
    sit on a bank account, statement credits on the card itself. Balances come from
    config (Plaid does not report reward balances); earned-after is an estimate that
    adds everything earned since the balance date, not the issuer's number.
    """
    today = today or date.today()
    end = end or today.isoformat()
    start = start or (today - timedelta(days=DEFAULT_REWARDS_SPAN)).isoformat()
    cards = []
    for c in cfg.rewards_cards:
        cards.append({**c, "account_id": select_account(conn, c["card"])})
    rows = classify(conn, cfg, today=today)
    purchases = [r for r in rows if r.cls == "spending" and r.role == "card" and r.amount > 0]
    out = []
    tot_e, tot_r = 0.0, 0.0
    for c in cards:
        span = [r for r in purchases if r.account_id == c["account_id"]
                and start <= r.date <= end and r.match_status != "refunded"]
        per_month: dict[str, float] = {}
        est_rows = []
        for r in span:
            rate, label = _cashback_rate(c, r)
            cb = round(r.amount * rate, 2)
            per_month[r.date[:7]] = per_month.get(r.date[:7], 0.0) + cb
            est_rows.append({"transaction_id": r.transaction_id, "date": r.date, "merchant": r.merchant,
                             "amount": round(r.amount, 2), "rate": rate, "label": label, "cashback": cb})
        estimated = round(sum(per_month.values()), 2)
        redeemed = round(-sum(r.amount for r in rows if r.cls == "other_income"
                               and r.account_id == c["account_id"]
                               and _match(cfg.redemption_patterns, r.name)
                               and start <= r.date <= end), 2)
        # projection uses everything earned/redeemed since the hand-entered balance date,
        # even when that date is older than the displayed span
        since = c.get("balance_as_of")
        since_est = round(sum(round(r.amount * _cashback_rate(c, r)[0], 2) for r in purchases
                              if r.account_id == c["account_id"] and r.match_status != "refunded"
                              and (not since or r.date >= since) and r.date <= end), 2)
        since_red = round(-sum(r.amount for r in rows if r.cls == "other_income"
                               and r.account_id == c["account_id"]
                               and _match(cfg.redemption_patterns, r.name)
                               and (not since or r.date >= since) and r.date <= end), 2)
        earned_after = (round(c["balance"] + since_est - since_red, 2)
                        if c.get("balance") is not None else None)
        out.append({"card": c["card"], "label": select_label(conn, c["card"]),
                    "base": c["base"], "bonus": [{"label": b["label"], "rate": b["rate"]} for b in c["bonus"]],
                    "purchases": len(span), "estimated": estimated, "redeemed": redeemed,
                    "per_month": {m: round(v, 2) for m, v in sorted(per_month.items())},
                    "balance": c.get("balance"), "balance_as_of": c.get("balance_as_of"),
                    "earned_after": earned_after, "earned_since": since_est, "redeemed_since": since_red,
                    "rows": est_rows})
        tot_e += estimated
        tot_r += redeemed
    # deposit redemptions (e.g. a card's rewards paid into the bank) not tied to one card above
    bank_cards = {c["account_id"] for c in cards}
    deposit_red = round(-sum(r.amount for r in rows if r.cls == "other_income"
                             and r.role != "card" and _match(cfg.redemption_patterns, r.name)), 2)
    return {"start": start, "end": end,
            "cards": out, "estimated_total": round(tot_e, 2), "redeemed_cards": round(tot_r, 2),
            "redeemed_deposits": deposit_red,
            "redeemed_total": round(tot_r + deposit_red, 2),
            "estimated_span_days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1}


def select_label(conn: sqlite3.Connection, sel: str) -> str:
    inst, _, key = sel.rpartition(":")  # last colon: institution names may contain ':'
    r = conn.execute("SELECT a.name, a.mask FROM accounts a JOIN items i USING(item_id) "
                     "WHERE i.institution_name = ? AND (a.mask = ? OR a.name = ?)", (inst, key, key)).fetchone()
    return r["name"] if r else sel
SYNC_STALE_HOURS = 36


def _month_end(month: str) -> date:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, monthrange(y, m)[1])


def accounts(conn: sqlite3.Connection, cfg: WaterfallConfig) -> list[dict]:
    """Every synced account with its role, label, balance, and card limit/statement."""
    roles = resolve_roles(conn, cfg)
    limits: dict[str, float] = {}
    for sel, lim in cfg.card_limits.items():
        limits[select_account(conn, sel)] = lim
    manual = {select_account(conn, sel) for sel in cfg.manual_pay}
    liab = {r["account_id"]: dict(r) for r in conn.execute("SELECT * FROM credit_liabilities")}
    out = []
    for r in conn.execute(
            "SELECT a.*, i.institution_name AS inst FROM accounts a JOIN items i USING(item_id) "
            "ORDER BY CASE a.type WHEN 'depository' THEN 0 WHEN 'investment' THEN 1 ELSE 2 END, "
            "i.institution_name, a.name"):
        aid = r["account_id"]
        credit = r["type"] == "credit"
        a = {"account_id": aid, "institution": r["inst"], "name": r["name"] or r["official_name"] or "",
             "mask": r["mask"], "type": r["type"], "subtype": r["subtype"], "role": roles.get(aid, ""),
             "current": r["current"], "updated_at": r["updated_at"],
             "label": (r["name"] or r["official_name"] or "") + (f" ··{r['mask']}" if r["mask"] else "")}
        if credit:
            lim = r["credit_limit"] if r["credit_limit"] is not None else limits.get(aid)
            st = liab.get(aid, {})
            a.update({"owed": r["current"] or 0.0, "limit": lim,
                      "limit_source": "plaid" if r["credit_limit"] is not None else ("config" if lim else None),
                      "manual_pay": aid in manual,
                      "statement_balance": st.get("last_statement_balance"),
                      "statement_date": st.get("last_statement_date"),
                      "minimum_payment": st.get("minimum_payment"),
                      "due_date": st.get("next_payment_due_date")})
        out.append(a)
    return out


def _bucket_of(a: dict) -> str | None:
    if a["type"] == "credit":
        return "cards"
    if a["type"] == "depository":
        return "cash"
    if a["type"] == "investment":
        return {"roth": "roth", "retirement": "retirement"}.get(a["role"], "brokerage")
    return None


def retirement_estimates(conn: sqlite3.Connection, accts: list[dict], rows: list) -> None:
    """Half-synced 401(k): while a retirement account's login is broken, estimate it.

    Contributions arrive with each paycheck at a fixed amount, so every regular
    paycheck dated after both the last real contribution and the last reported
    balance adds that last contribution again. Market moves are not estimated.
    Sets ``a["estimate"]`` (and leaves ``current`` untouched); the first healthy
    sync replaces it with real data.
    """
    from .report import item_health
    health = {r[0]: item_health(json.loads(r[1] or "{}")) for r in
              conn.execute("SELECT item_id, products_json FROM items")}
    item_of = {r[0]: r[1] for r in conn.execute("SELECT account_id, item_id FROM accounts")}
    paydays = sorted({r.date for r in rows if r.cls == "paycheck"
                      and (not r.bonus_candidate or r.bonus_decision == "paycheck")})
    for a in accts:
        if a["role"] != "retirement" or a["current"] is None:
            continue
        h = health.get(item_of.get(a["account_id"]), {})
        if h.get("state") in (None, "ok", "expiring"):
            continue
        last = conn.execute(
            "SELECT date, SUM(amount) FROM investment_transactions WHERE account_id=? AND subtype='contribution' "
            "GROUP BY date ORDER BY date DESC LIMIT 1", (a["account_id"],)).fetchone()
        if not last or not last[1]:
            continue
        snap = conn.execute("SELECT MAX(date) FROM balance_snapshots WHERE account_id=? AND current IS NOT NULL",
                            (a["account_id"],)).fetchone()[0]
        since = max(last[0], snap or last[0])
        missed = [d for d in paydays if d > since]
        if not missed:
            continue
        per = round(abs(last[1]), 2)
        a["estimate"] = {"reason": h.get("reason") or h.get("state"), "as_of": since,
                         "per_paycheck": per, "basis_date": last[0], "paychecks": missed,
                         "added": round(per * len(missed), 2),
                         "current": round(a["current"] + per * len(missed), 2)}


def net_worth(accts: list[dict]) -> dict:
    """All cash and investment accounts (ignored ones included) minus card balances owed.

    A half-synced 401(k) counts at its estimate (``estimated`` lists those accounts).
    """
    parts = {"cash": 0.0, "brokerage": 0.0, "roth": 0.0, "retirement": 0.0, "cards": 0.0}
    estimated = []
    for a in accts:
        b = _bucket_of(a)
        if b is None or a["current"] is None:
            continue
        cur = a["current"]
        if a.get("estimate"):
            cur = a["estimate"]["current"]
            estimated.append(a["label"])
        parts[b] += -max(cur, 0.0) if b == "cards" else cur
    parts = {k: round(v, 2) for k, v in parts.items()}
    out = {"total": round(sum(parts.values()), 2), "parts": parts}
    if estimated:
        out["estimated"] = estimated
    return out


def net_worth_history(conn: sqlite3.Connection, accts: list[dict]) -> list[dict]:
    """Daily net worth from balance snapshots, carrying each account's last value forward.

    A day is included only once every current account has reported at least once,
    so the line never jumps because an account appeared.
    """
    kinds = {a["account_id"]: _bucket_of(a) for a in accts if _bucket_of(a)}
    snaps: dict[str, dict[str, float]] = {}
    for r in conn.execute("SELECT account_id, date, current FROM balance_snapshots "
                          "WHERE current IS NOT NULL ORDER BY date"):
        if r[0] in kinds:
            snaps.setdefault(r[1], {})[r[0]] = r[2]
    last: dict[str, float] = {}
    out = []
    for d in sorted(snaps):
        last.update(snaps[d])
        if len(last) < len(kinds):
            continue
        total = sum(-max(v, 0.0) if kinds[a] == "cards" else v for a, v in last.items())
        out.append({"date": d, "total": round(total, 2)})
    return out


def holdings(conn: sqlite3.Connection, accts: list[dict]) -> dict:
    names = {a["account_id"]: a for a in accts}
    rows = []
    for r in conn.execute(
            "SELECT h.account_id, h.quantity, h.institution_value AS value, h.cost_basis, "
            "s.ticker, s.name, s.type FROM holdings h LEFT JOIN securities s USING(security_id)"):
        if r["value"] is None:
            continue
        a = names.get(r["account_id"], {})
        rows.append({"ticker": r["ticker"], "name": r["name"] or r["ticker"] or "unknown",
                     "type": r["type"], "account": a.get("label", ""), "role": a.get("role", ""),
                     "value": round(r["value"], 2),
                     "gain": round(r["value"] - r["cost_basis"], 2) if r["cost_basis"] is not None else None})
    total = sum(h["value"] for h in rows)
    rows.sort(key=lambda h: -h["value"])
    for h in rows:
        h["weight"] = round(h["value"] / total, 4) if total else 0.0
    by_type: dict[str, float] = {}
    for h in rows:
        by_type[h["type"] or "other"] = by_type.get(h["type"] or "other", 0.0) + h["value"]
    return {"total": round(total, 2), "positions": rows,
            "by_type": {k: round(v, 2) for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])}}


def roth_plan(conn: sqlite3.Connection, cfg: WaterfallConfig, today: date) -> dict:
    """Current tax year's Roth progress and per-close installment schedule."""
    month = today.isoformat()[:7]
    y = tax_year(month)
    limit = cfg.roth_limits.get(y)
    if limit is None:
        return {"tax_year": y, "limit": None, "error": f"no Roth limit configured for tax year {y}"}
    skips = skipped_months(conn)
    done = contributed(conn, y)
    remaining = round(max(0.0, limit - done), 2)
    window = window_from(month)
    open_months = [m for m in window if m not in skips]
    amounts = dict(zip(open_months, split_installments(remaining, len(open_months))))
    each = amounts[open_months[0]] if open_months else 0.0
    last = conn.execute("SELECT date, amount FROM roth_contributions WHERE tax_year=? "
                        "ORDER BY date DESC, id DESC LIMIT 1", (y,)).fetchone()
    return {"tax_year": y, "limit": limit, "contributed": done, "remaining": remaining,
            "closes_left": len(open_months), "installment": each,
            "deadline": f"{y + 1}-04-15",
            "schedule": [{"month": m, "skipped": m in skips, "amount": amounts.get(m, 0.0)}
                         for m in window],
            "last": {"date": last[0], "amount": last[1]} if last else None}


def overview(conn: sqlite3.Connection, cfg: WaterfallConfig, items: list[dict],
             today: date | None = None) -> dict:
    """Everything the Overview tab shows, in one call."""
    today = today or date.today()
    accts = accounts(conn, cfg)
    ms = close_months(conn, cfg, today)
    rows = classify(conn, cfg, today=today)
    retirement_estimates(conn, accts, rows)

    flow = []
    for m in sorted(ms, key=lambda x: x["month"])[-12:]:
        f = m.get("frozen") if m["status"] == "closed" and "fixed" in (m.get("frozen") or {}) else m["figures"]
        flow.append({"month": m["month"], "partial": m["partial_history"], "status": m["status"],
                     **{k: f.get(k, 0.0) for k in ("paychecks", "spending", "subscriptions", "fixed",
                                                   "outflows", "surplus", "bonus")}})

    rec_month = next((m for m in ms if m["recommendation"]), None)
    open_m = next((m for m in sorted(ms, key=lambda x: x["month"]) if m["status"] != "closed"
                   and not m["partial_history"]), None)
    next_close = None
    if open_m:
        next_close = {"month": open_m["month"],
                      "date": (_month_end(open_m["month"]) + timedelta(days=cfg.close_grace_days + 1)).isoformat(),
                      "status": open_m["status"]}

    attention, upcoming = [], []
    n_review = sum(needs_review(r) for r in rows)
    if n_review:
        attention.append({"level": "warn", "text": f"{n_review} transaction(s) to review", "tab": "admin"})
    for it in items:
        h = it.get("health") or {}
        if h.get("state") not in (None, "ok"):
            attention.append({"level": "bad" if h["state"] != "expiring" else "warn",
                              "text": f"{it['institution']}: {h.get('reason') or h['state']}", "tab": "admin"})
    for a in accts:
        e = a.get("estimate")
        if e:
            attention.append({"level": "warn", "tab": "admin",
                              "text": f"{a['label']}: estimated, {len(e['paychecks'])} paycheck contribution(s) "
                                      f"added since {e['as_of']}; re-connect for real numbers"})
    synced = [it["last_sync"] for it in items if it.get("last_sync")]
    if synced:
        newest = max(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in synced)
        age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        if age_h > SYNC_STALE_HOURS:
            attention.append({"level": "warn", "text": f"last sync {age_h / 24:.0f} days ago", "tab": "admin"})
    for m in ms:
        if m["status"] == "closeable":
            attention.append({"level": "ok", "text": f"{m['month']} is ready to close", "tab": "close"})
        for b in m["bonus_candidates"]:
            if b["decision"] is None:
                attention.append({"level": "warn", "text": f"possible bonus on {b['date']} needs a decision",
                                  "tab": "close"})
    for a in accts:
        if a["type"] != "credit" or not a.get("due_date"):
            continue
        days = (date.fromisoformat(a["due_date"]) - today).days
        due_amt = a.get("statement_balance") or 0.0
        upcoming.append({"date": a["due_date"], "kind": "card", "label": f"{a['label']} payment",
                         "amount": due_amt, "minimum": a.get("minimum_payment"),
                         "manual": a["manual_pay"], "days": days})
        if a["manual_pay"] and due_amt > 0 and days <= MANUAL_DUE_WARN_DAYS:
            attention.insert(0, {"level": "bad" if days < 0 else "warn",
                                 "text": (f"{a['label']} payment overdue" if days < 0 else
                                          f"{a['label']} payment due in {days} day(s), no autopay"),
                                 "tab": "overview"})
    if next_close and next_close["date"] >= today.isoformat():  # once due, "ready to close" covers it
        upcoming.append({"date": next_close["date"], "kind": "close",
                         "label": f"{next_close['month']} close", "amount": None,
                         "days": (date.fromisoformat(next_close["date"]) - today).days})
    for e in cfg.open_earmarks:
        if e["due"]:
            d = e["due"] + "-01" if len(e["due"]) == 7 else e["due"]
            upcoming.append({"date": d, "kind": "earmark", "label": f"{e['name']} (earmarked)",
                             "amount": e["amount"], "days": (date.fromisoformat(d) - today).days,
                             "month_only": len(e["due"]) == 7, "note": e["note"]})
    roth = roth_plan(conn, cfg, today)
    if roth.get("deadline"):
        upcoming.append({"date": roth["deadline"], "kind": "roth", "label": f"Roth TY{roth['tax_year']} deadline",
                         "amount": roth.get("remaining"),
                         "days": (date.fromisoformat(roth["deadline"]) - today).days})
    upcoming.sort(key=lambda u: u["date"])

    cur_month = today.isoformat()[:7]
    this_m = next((m for m in ms if m["month"] == cur_month), None)
    return {
        "today": today.isoformat(),
        "accounts": accts,
        "net_worth": net_worth(accts) | {"history": net_worth_history(conn, accts)},
        "holdings": holdings(conn, accts),
        "cash_flow": flow,
        "recommendation": ({"month": rec_month["month"], "status": rec_month["status"],
                            "balance_as_of": rec_month.get("balance_as_of")
                            or (rec_month.get("month_end_balance") or {}).get("date"),
                            "cards_owed": rec_month["cards_owed"], **rec_month["recommendation"]}
                           if rec_month else None),
        "next_close": next_close,
        "roth": roth,
        "attention": attention,
        "upcoming": upcoming,
        "this_month": ({"month": cur_month, **{k: this_m["figures"][k] for k in
                        ("spending", "subscriptions", "fixed", "refunds")},
                        "categories": this_m["figures"]["categories"]} if this_m else None),
        "earmarks": [dict(e) for e in cfg.earmarks],
    }


def spending(conn: sqlite3.Connection, cfg: WaterfallConfig, start: str, end: str,
             buckets: tuple[str, ...] = ("spending",), account: str | None = None,
             category: tuple[str, ...] = (), q: str = "", today: date | None = None) -> dict:
    """Purchases and refunds between start and end (inclusive), filtered.

    Amounts are signed the human way: a purchase is negative, a refund positive.
    Fully refunded pairs are returned (flagged ``refunded``) but left out of totals.
    Roommate reimbursements show up under ``fixed`` because they repay rent/bills.
    Each row on a card in [rewards] carries an estimated ``cashback`` (rate +
    dollars), computed with the same rules as the Rewards panel.
    """
    try:
        d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as e:
        raise WaterfallConfigError("start and end must be YYYY-MM-DD") from e
    if d1 < d0:
        raise WaterfallConfigError("end is before start")
    accts = {a["account_id"]: a for a in accounts(conn, cfg)}
    rows = classify(conn, cfg, today=today)
    ql = q.strip().lower()

    def keep(r, lo: str, hi: str) -> bool:
        b = "fixed" if r.cls == "reimbursement" else r.bucket
        if not b or b not in buckets or not lo <= r.date <= hi:
            return False
        cat = "reimbursed" if r.cls == "reimbursement" else r.category
        if account and r.account_id != account:
            return False
        if category and cat not in category:
            return False
        return not ql or ql in r.merchant.lower() or ql in r.name.lower()

    sel = [r for r in rows if keep(r, d0.isoformat(), d1.isoformat())]
    span = (d1 - d0).days + 1
    p0, p1 = d0 - timedelta(days=span), d0 - timedelta(days=1)
    prev = [r for r in rows if keep(r, p0.isoformat(), p1.isoformat())]

    def spent(rs) -> float:
        return round(sum(r.amount for r in rs if r.match_status != "refunded"), 2)

    cats: dict[str, float] = {}
    for r in sel:
        if r.match_status == "refunded":
            continue
        k = "reimbursed" if r.cls == "reimbursement" else r.category
        cats[k] = cats.get(k, 0.0) + r.amount
    counted = [r for r in sel if r.match_status != "refunded"]
    purchases = [r for r in counted if r.amount > 0]
    # estimated cash back per purchase on cards in [rewards] — same rules as the Rewards panel
    rcards = {}
    for c in cfg.rewards_cards:
        if aid := select_account(conn, c["card"]):
            rcards[aid] = c

    def cashback(r):
        c = rcards.get(r.account_id)
        if not c or r.cls != "spending" or r.role != "card" or r.amount <= 0 or r.match_status == "refunded":
            return None
        rate, label = _cashback_rate(c, r)
        return {"rate": rate, "label": label, "cashback": round(r.amount * rate, 2)}
    all_cats = sorted({("reimbursed" if r.cls == "reimbursement" else r.category)
                       for r in rows if (r.bucket or r.cls == "reimbursement")})
    return {
        "start": d0.isoformat(), "end": d1.isoformat(), "buckets": list(buckets),
        "spent": spent(sel), "previous": {"start": p0.isoformat(), "end": p1.isoformat(), "spent": spent(prev)},
        "purchases": len(purchases),
        "average": round(sum(r.amount for r in purchases) / len(purchases), 2) if purchases else 0.0,
        "refunds": round(-sum(r.amount for r in counted if r.amount < 0), 2),
        "refunded_pairs": sum(r.match_status == "refunded" and r.amount > 0 for r in sel),
        "categories": {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])},
        "all_categories": all_cats,
        "rows": [{"transaction_id": r.transaction_id, "date": r.date, "merchant": r.merchant, "name": r.name,
                  "category": "reimbursed" if r.cls == "reimbursement" else r.category,
                  "bucket": "fixed" if r.cls == "reimbursement" else r.bucket,
                  "account": accts.get(r.account_id, {}).get("label", r.role), "account_id": r.account_id,
                  "amount": round(-r.amount, 2), "cashback": cashback(r),
                  "refunded": r.match_status == "refunded",
                  "pair": r.match_with if r.match_status == "refunded" else None}
                 for r in sorted(sel, key=lambda r: (r.date, r.transaction_id), reverse=True)],
    }


"""Coverage report: what each connected institution actually returns.

Deliberately contains no balances, amounts, account numbers or merchant names,
so it is safe to paste into docs. Masks are omitted too.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .db import DB

STATUS_ICON = {"ok": "yes", "not_supported": "no", "not_ready": "pending", "error": "ERROR"}

# Item errors that update mode (re-connect) fixes.
RECONNECT_ERRORS = {"ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT",
                    "INVALID_CREDENTIALS", "INSUFFICIENT_CREDENTIALS", "ITEM_LOCKED",
                    "USER_PERMISSION_REVOKED", "ACCESS_NOT_GRANTED"}
CONSENT_WARN_DAYS = 30


def item_health(meta: dict, now: datetime | None = None) -> dict:
    """ok | expiring | reconnect, plus a human reason."""
    now = now or datetime.now(timezone.utc)
    err = meta.get("item_error")
    if err in RECONNECT_ERRORS:
        return {"state": "reconnect", "reason": f"Plaid reports {err}"}
    if err:
        return {"state": "error", "reason": f"Plaid reports {err}"}
    exp = meta.get("consent_expiration_time")
    if exp:
        try:
            when = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            days = (when - now).days
            if days < 0:
                return {"state": "reconnect", "reason": f"consent expired {exp[:10]}"}
            if days <= CONSENT_WARN_DAYS:
                return {"state": "expiring", "reason": f"consent expires {exp[:10]} ({days} days)"}
            return {"state": "ok", "reason": f"consent valid until {exp[:10]}"}
    if "consent_expiration_time" in meta:
        # Non-OAuth (credential) login: no 12-month renewal, but a password
        # change at the institution breaks it until re-connected.
        return {"state": "ok", "reason": "no consent expiry (credential login)"}
    return {"state": "ok", "reason": ""}


def build(db: DB) -> dict:
    items = []
    for it in db.q("SELECT * FROM items ORDER BY created_at"):
        iid = it["item_id"]
        accounts = db.q("SELECT type, subtype FROM accounts WHERE item_id=? ORDER BY type, subtype", iid)
        results = {r["product"]: dict(r) for r in db.q("SELECT * FROM sync_results WHERE item_id=?", iid)}
        txn = db.one(
            """SELECT COUNT(*) n, MIN(date) first, MAX(date) last, SUM(pending) pending
               FROM transactions WHERE item_id=?""", iid)
        txn_counts = json.loads((results.get("transactions") or {}).get("counts_json") or "{}")
        inv = db.one(
            "SELECT COUNT(*) n, MIN(date) first, MAX(date) last FROM investment_transactions WHERE item_id=?", iid)
        hold = db.one(
            """SELECT COUNT(*) n, COUNT(DISTINCT security_id) secs, MAX(price_as_of) price_as_of,
                      SUM(cost_basis IS NOT NULL) with_basis
               FROM holdings WHERE item_id=?""", iid)
        liab = db.one("SELECT COUNT(*) n FROM credit_liabilities WHERE item_id=?", iid)
        meta = json.loads(it["products_json"] or "{}")
        items.append({
            "item_id": iid,
            "health": item_health(meta),
            "institution": it["institution_name"] or it["institution_id"] or "unknown",
            "link_kind": it["link_kind"],
            "connected": it["created_at"],
            "last_sync": it["last_sync_at"],
            "account_types": [f"{a['type']}/{a['subtype']}" for a in accounts],
            "products": {
                p: {"status": r["status"], "detail": r["detail"]} for p, r in results.items()
            },
            "transactions": _span(txn) | {"pending": txn["pending"] or 0,
                                          "update_status": txn_counts.get("update_status")},
            "investment_transactions": _span(inv),
            "holdings": {"positions": hold["n"], "securities": hold["secs"],
                          "with_cost_basis": hold["with_basis"] or 0,
                          "latest_price_date": hold["price_as_of"]},
            "credit_liabilities": liab["n"],
            "plaid_item": {k: meta.get(k) for k in (
                "billed_products", "available_products", "consented_products",
                "item_error", "consent_expiration_time", "update_type", "last_successful_update")},
        })
    return {"items_ever_created": db.items_ever_created(), "items": items}


def _span(row) -> dict:
    out = {"count": row["n"], "first": row["first"], "last": row["last"]}
    if row["first"] and row["last"]:
        out["days_of_history"] = (date.fromisoformat(row["last"]) - date.fromisoformat(row["first"])).days
        out["days_since_latest"] = (date.today() - date.fromisoformat(row["last"])).days
    return out


def to_markdown(rep: dict, plaid_env: str) -> str:
    lines = [f"# Weyland coverage report ({plaid_env})", "",
             f"Items ever created in this environment: **{rep['items_ever_created']}**", ""]
    if not rep["items"]:
        lines.append("_No institutions connected yet._")
        return "\n".join(lines) + "\n"
    prods = ["accounts", "transactions", "liabilities", "investments", "investment_transactions"]
    lines.append("| Institution | Accounts | " + " | ".join(prods) + " |")
    lines.append("|---|---|" + "---|" * len(prods))
    for it in rep["items"]:
        cells = [STATUS_ICON.get(it["products"].get(p, {}).get("status", ""), "-") for p in prods]
        lines.append(f"| {it['institution']} | {', '.join(it['account_types']) or '-'} | " + " | ".join(cells) + " |")
    for it in rep["items"]:
        t, i, h = it["transactions"], it["investment_transactions"], it["holdings"]
        lines += ["", f"## {it['institution']}", "",
                  f"- Connected {it['connected']} via `{it['link_kind']}` link; last sync {it['last_sync']}",
                  f"- Transactions: {t['count']} ({t.get('days_of_history', 0)} days, "
                  f"{t['first']} → {t['last']}, {t['pending']} pending, latest {t.get('days_since_latest', '-')} days ago; "
                  f"Plaid status `{t.get('update_status')}`)"
                  + (" — **history still loading, re-sync later**"
                     if t.get("update_status") not in (None, "HISTORICAL_UPDATE_COMPLETE") else ""),
                  f"- Investment transactions: {i['count']} ({i['first']} → {i['last']})",
                  f"- Holdings: {h['positions']} positions / {h['securities']} securities, "
                  f"{h['with_cost_basis']} with cost basis, prices as of {h['latest_price_date']}",
                  f"- Credit liability records: {it['credit_liabilities']}"]
        meta = it["plaid_item"]
        if meta.get("billed_products") is not None:
            lines.append(f"- Plaid products billed/available: {meta['billed_products']} / {meta['available_products']}")
        if meta.get("consent_expiration_time"):
            lines.append(f"- **Consent expires {meta['consent_expiration_time']}** (re-connect from the UI; does not use a slot)")
        if it["health"]["state"] != "ok":
            lines.append(f"- **Needs attention: {it['health']['state']} ({it['health']['reason']})**")
        if meta.get("item_error"):
            lines.append(f"- **Item error: {meta['item_error']}**")
        for p, r in it["products"].items():
            if r["status"] != "ok" and r["detail"]:
                lines.append(f"- {p}: {r['status']}: `{r['detail']}`")
    return "\n".join(lines) + "\n"

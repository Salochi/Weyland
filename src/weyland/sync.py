"""Pull data from Plaid into SQLite.

Each product is synced independently so one unsupported product (e.g. a 401k
with no transactions) never blocks the others. Outcomes land in sync_results,
which the coverage report reads.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Any

from .crypto import TokenBox
from .db import DB, now_iso
from .plaid_client import PlaidClient, PlaidError

# Plaid error codes that mean "this Item can't do that", not "something broke".
NOT_SUPPORTED = {
    "PRODUCTS_NOT_SUPPORTED",
    "PRODUCT_NOT_ENABLED",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_INVESTMENT_AUTH_ACCOUNTS",
    "NO_LIABILITY_ACCOUNTS",
    "NO_ACCOUNTS",
    "ADDITIONAL_CONSENT_REQUIRED",
    "INVALID_PRODUCT",
}
NOT_READY = {"PRODUCT_NOT_READY"}

PRODUCTS = ("accounts", "transactions", "investments", "investment_transactions", "liabilities")

# Only sync what each link kind consented to. Asking a bank Item for
# investments (or a brokerage for liabilities) just returns
# ADDITIONAL_CONSENT_REQUIRED and clutters the coverage report.
PRODUCTS_BY_KIND = {
    "bank": ("accounts", "transactions", "liabilities"),
    "investment": ("accounts", "transactions", "investments", "investment_transactions"),
}


def products_for(link_kind: str | None) -> tuple[str, ...]:
    """Products to sync for an Item; unknown kinds try everything."""
    return PRODUCTS_BY_KIND.get(link_kind or "", PRODUCTS)


def _classify(err: PlaidError) -> str:
    if err.error_code in NOT_SUPPORTED:
        return "not_supported"
    if err.error_code in NOT_READY:
        return "not_ready"
    return "error"


def _err_detail(err: PlaidError) -> str:
    # error_message is Plaid's text; it never contains our secret or token.
    return f"{err.error_type}/{err.error_code}: {err.error_message}"[:300]


class Syncer:
    def __init__(self, db: DB, plaid: PlaidClient, box: TokenBox):
        self.db = db
        self.plaid = plaid
        self.box = box

    # ------------------------------------------------------------------ public
    def sync_all(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        for row in self.db.q("SELECT item_id FROM items ORDER BY created_at"):
            out[row["item_id"]] = self.sync_item(row["item_id"])
        return out

    def sync_item(self, item_id: str) -> dict[str, str]:
        row = self.db.one("SELECT * FROM items WHERE item_id=?", item_id)
        if row is None:
            raise KeyError(item_id)
        token = self.box.open(row["access_token_enc"])
        results: dict[str, str] = {}
        wanted = products_for(row["link_kind"])
        # Drop stale outcomes for products this kind no longer syncs.
        self.db.conn.execute(
            f"DELETE FROM sync_results WHERE item_id=? AND product NOT IN ({','.join('?' * len(wanted))})",
            (item_id, *wanted),
        )
        for product in wanted:
            fn = getattr(self, f"_sync_{product}")
            try:
                counts = fn(item_id, token, row)
                self.db.set_result(item_id, product, "ok", None, counts)
                results[product] = "ok"
            except PlaidError as err:
                status = _classify(err)
                self.db.set_result(item_id, product, status, _err_detail(err))
                results[product] = status
                if err.error_code == "ITEM_LOGIN_REQUIRED":
                    # Every other product will fail the same way; record it and stop.
                    break
        self._sync_item_status(item_id, token)
        self.db.conn.execute(
            "UPDATE items SET last_sync_at=?, last_error=? WHERE item_id=?",
            (now_iso(), None if "error" not in results.values() else json.dumps(results), item_id),
        )
        return results

    # ---------------------------------------------------------------- products
    def _sync_item_status(self, item_id: str, token: str) -> None:
        try:
            data = self.plaid.call("/item/get", access_token=token)
        except PlaidError:
            return
        item = data.get("item", {})
        status = data.get("status", {}) or {}
        self.db.conn.execute(
            "UPDATE items SET products_json=? WHERE item_id=?",
            (json.dumps({
                "billed_products": item.get("billed_products"),
                "available_products": item.get("available_products"),
                "consented_products": item.get("consented_products"),
                "item_error": (item.get("error") or {}).get("error_code"),
                "consent_expiration_time": item.get("consent_expiration_time"),
                "update_type": item.get("update_type"),
                "last_successful_update": {
                    k: (v or {}).get("last_successful_update")
                    for k, v in status.items() if isinstance(v, dict)
                },
            }), item_id),
        )

    def _sync_accounts(self, item_id: str, token: str, _row) -> dict[str, Any]:
        data = self.plaid.call("/accounts/get", access_token=token)
        self._store_accounts(item_id, data.get("accounts", []))
        return {"accounts": len(data.get("accounts", []))}

    def _store_accounts(self, item_id: str, accounts: list[dict]) -> None:
        ts = now_iso()
        with self.db.tx() as c:
            for a in accounts:
                b = a.get("balances", {}) or {}
                c.execute(
                    """INSERT INTO accounts(account_id,item_id,name,official_name,mask,type,subtype,
                                            current,available,credit_limit,iso_currency,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id) DO UPDATE SET name=excluded.name,
                         official_name=excluded.official_name, mask=excluded.mask, type=excluded.type,
                         subtype=excluded.subtype, current=excluded.current, available=excluded.available,
                         credit_limit=excluded.credit_limit, iso_currency=excluded.iso_currency,
                         updated_at=excluded.updated_at""",
                    (a["account_id"], item_id, a.get("name"), a.get("official_name"), a.get("mask"),
                     a.get("type"), a.get("subtype"), b.get("current"), b.get("available"),
                     b.get("limit"), b.get("iso_currency_code"), ts),
                )
                c.execute(
                    """INSERT INTO balance_snapshots(account_id, date, current, available, at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(account_id, date) DO UPDATE SET current=excluded.current,
                         available=excluded.available, at=excluded.at""",
                    (a["account_id"], date.today().isoformat(), b.get("current"), b.get("available"), ts),
                )

    def _sync_transactions(self, item_id: str, token: str, row) -> dict[str, Any]:
        cursor = row["transactions_cursor"]
        added = modified = removed = 0
        # The first sync on a fresh Item can return an empty page while Plaid is
        # still extracting history; give it a short grace period.
        empty_retries = 4 if not cursor else 0
        while True:
            body: dict[str, Any] = {"access_token": token, "count": 500}
            if cursor:
                body["cursor"] = cursor
            data = self.plaid.call("/transactions/sync", **body)
            page_has = data.get("added") or data.get("modified") or data.get("removed")
            if not page_has and not cursor and empty_retries > 0 and not data.get("has_more"):
                status = data.get("transactions_update_status")
                if status in (None, "NOT_READY"):
                    empty_retries -= 1
                    time.sleep(3)
                    continue
            ts = now_iso()
            with self.db.tx() as c:
                for t in data.get("added", []) + data.get("modified", []):
                    pfc = t.get("personal_finance_category") or {}
                    c.execute(
                        """INSERT INTO transactions(transaction_id,account_id,item_id,date,authorized_date,
                               amount,iso_currency,name,merchant_name,pending,category_primary,
                               category_detail,payment_channel,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(transaction_id) DO UPDATE SET date=excluded.date,
                             authorized_date=excluded.authorized_date, amount=excluded.amount,
                             name=excluded.name, merchant_name=excluded.merchant_name,
                             pending=excluded.pending, category_primary=excluded.category_primary,
                             category_detail=excluded.category_detail,
                             payment_channel=excluded.payment_channel, updated_at=excluded.updated_at""",
                        (t["transaction_id"], t["account_id"], item_id, t.get("date"),
                         t.get("authorized_date"), t.get("amount"), t.get("iso_currency_code"),
                         t.get("name"), t.get("merchant_name"), int(bool(t.get("pending"))),
                         pfc.get("primary"), pfc.get("detailed"), t.get("payment_channel"), ts),
                    )
                for t in data.get("removed", []):
                    c.execute("DELETE FROM transactions WHERE transaction_id=?", (t["transaction_id"],))
                cursor = data.get("next_cursor")
                c.execute("UPDATE items SET transactions_cursor=? WHERE item_id=?", (cursor, item_id))
            added += len(data.get("added", []))
            modified += len(data.get("modified", []))
            removed += len(data.get("removed", []))
            if data.get("accounts"):
                self._store_accounts(item_id, data["accounts"])
            if not data.get("has_more"):
                return {"added": added, "modified": modified, "removed": removed,
                        "update_status": data.get("transactions_update_status")}

    def _sync_investments(self, item_id: str, token: str, _row) -> dict[str, Any]:
        data = self.plaid.call("/investments/holdings/get", access_token=token)
        self._store_accounts(item_id, data.get("accounts", []))
        self._store_securities(data.get("securities", []))
        ts = now_iso()
        with self.db.tx() as c:
            acct_ids = {h["account_id"] for h in data.get("holdings", [])}
            for aid in acct_ids:
                c.execute("DELETE FROM holdings WHERE account_id=?", (aid,))
            for h in data.get("holdings", []):
                c.execute(
                    """INSERT INTO holdings(account_id,security_id,item_id,quantity,institution_price,
                           institution_value,cost_basis,price_as_of,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (h["account_id"], h["security_id"], item_id, h.get("quantity"),
                     h.get("institution_price"), h.get("institution_value"), h.get("cost_basis"),
                     h.get("institution_price_as_of"), ts),
                )
        return {"holdings": len(data.get("holdings", [])), "securities": len(data.get("securities", []))}

    def _sync_investment_transactions(self, item_id: str, token: str, _row) -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=730)
        offset, total, stored = 0, None, 0
        while total is None or offset < total:
            data = self.plaid.call(
                "/investments/transactions/get", access_token=token,
                start_date=start.isoformat(), end_date=end.isoformat(),
                options={"count": 500, "offset": offset},
            )
            total = data.get("total_investment_transactions", 0)
            rows = data.get("investment_transactions", [])
            self._store_securities(data.get("securities", []))
            ts = now_iso()
            with self.db.tx() as c:
                for t in rows:
                    c.execute(
                        """INSERT INTO investment_transactions(investment_transaction_id,account_id,item_id,
                               security_id,date,name,amount,quantity,price,type,subtype,updated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(investment_transaction_id) DO UPDATE SET amount=excluded.amount,
                             quantity=excluded.quantity, price=excluded.price, type=excluded.type,
                             subtype=excluded.subtype, name=excluded.name, date=excluded.date,
                             updated_at=excluded.updated_at""",
                        (t["investment_transaction_id"], t["account_id"], item_id, t.get("security_id"),
                         t.get("date"), t.get("name"), t.get("amount"), t.get("quantity"),
                         t.get("price"), t.get("type"), t.get("subtype"), ts),
                    )
            stored += len(rows)
            if not rows:
                break
            offset += len(rows)
        return {"investment_transactions": stored}

    def _sync_liabilities(self, item_id: str, token: str, _row) -> dict[str, Any]:
        data = self.plaid.call("/liabilities/get", access_token=token)
        credit = (data.get("liabilities") or {}).get("credit") or []
        ts = now_iso()
        with self.db.tx() as c:
            for l in credit:
                c.execute(
                    """INSERT INTO credit_liabilities(account_id,item_id,last_statement_balance,
                           last_statement_date,minimum_payment,next_payment_due_date,aprs_json,updated_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_id) DO UPDATE SET
                         last_statement_balance=excluded.last_statement_balance,
                         last_statement_date=excluded.last_statement_date,
                         minimum_payment=excluded.minimum_payment,
                         next_payment_due_date=excluded.next_payment_due_date,
                         aprs_json=excluded.aprs_json, updated_at=excluded.updated_at""",
                    (l["account_id"], item_id, l.get("last_statement_balance"),
                     l.get("last_statement_issue_date"), l.get("minimum_payment_amount"),
                     l.get("next_payment_due_date"), json.dumps(l.get("aprs") or []), ts),
                )
        return {"credit_accounts": len(credit)}

    def _store_securities(self, securities: list[dict]) -> None:
        ts = now_iso()
        with self.db.tx() as c:
            for s in securities:
                c.execute(
                    """INSERT INTO securities(security_id,ticker,name,type,close_price,updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(security_id) DO UPDATE SET ticker=excluded.ticker, name=excluded.name,
                         type=excluded.type, close_price=excluded.close_price, updated_at=excluded.updated_at""",
                    (s["security_id"], s.get("ticker_symbol"), s.get("name"), s.get("type"),
                     s.get("close_price"), ts),
                )

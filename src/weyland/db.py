"""SQLite storage. One database per Plaid environment."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    item_id             TEXT PRIMARY KEY,
    institution_id      TEXT,
    institution_name    TEXT,
    link_kind           TEXT,
    access_token_enc    BLOB NOT NULL,
    transactions_cursor TEXT,
    products_json       TEXT,
    created_at          TEXT NOT NULL,
    last_sync_at        TEXT,
    last_error          TEXT
);

-- Every Item ever created in this environment. Plaid Trial caps Items at 10 and
-- /item/remove does NOT free a slot, so this table is append-only.
CREATE TABLE IF NOT EXISTS item_ledger (
    item_id          TEXT PRIMARY KEY,
    institution_name TEXT,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    name            TEXT,
    official_name   TEXT,
    mask            TEXT,
    type            TEXT,
    subtype         TEXT,
    current         REAL,
    available       REAL,
    credit_limit    REAL,
    iso_currency    TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id   TEXT PRIMARY KEY,
    account_id       TEXT NOT NULL,
    item_id          TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    date             TEXT,
    authorized_date  TEXT,
    amount           REAL,
    iso_currency     TEXT,
    name             TEXT,
    merchant_name    TEXT,
    pending          INTEGER,
    category_primary TEXT,
    category_detail  TEXT,
    payment_channel  TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_txn_account_date ON transactions(account_id, date);

CREATE TABLE IF NOT EXISTS securities (
    security_id TEXT PRIMARY KEY,
    ticker      TEXT,
    name        TEXT,
    type        TEXT,
    close_price REAL,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id        TEXT NOT NULL,
    security_id       TEXT NOT NULL,
    item_id           TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    quantity          REAL,
    institution_price REAL,
    institution_value REAL,
    cost_basis        REAL,
    price_as_of       TEXT,
    updated_at        TEXT,
    PRIMARY KEY (account_id, security_id)
);

CREATE TABLE IF NOT EXISTS txn_overrides (
    transaction_id TEXT PRIMARY KEY,
    class          TEXT NOT NULL,
    note           TEXT,
    at             TEXT NOT NULL
);

-- Waterfall bookkeeping (phase 4). Amounts live only here and in the
-- tailnet UI, never in the repo, reports or docs.
CREATE TABLE IF NOT EXISTS roth_contributions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    date      TEXT NOT NULL,              -- YYYY-MM-DD the money moved
    amount    REAL NOT NULL,
    tax_year  INTEGER NOT NULL,
    note      TEXT,
    at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roth_skips (
    month TEXT PRIMARY KEY,               -- YYYY-MM close that skips the Roth installment
    at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bonus_decisions (
    transaction_id TEXT PRIMARY KEY,
    decision       TEXT NOT NULL CHECK (decision IN ('bonus', 'paycheck')),
    at             TEXT NOT NULL
);

-- Frozen monthly close figures. Recomputed figures are compared against this
-- snapshot so later re-syncs can't rewrite history silently (drift warning).
CREATE TABLE IF NOT EXISTS month_closes (
    month          TEXT PRIMARY KEY,      -- YYYY-MM
    figures_json   TEXT NOT NULL,
    hysa_balance   REAL,                  -- HYSA balance at month end (see close.py)
    balance_as_of  TEXT,                  -- the month-end date the balance is for
    closed_at      TEXT NOT NULL
);

-- Plaid only returns the balance as of "now", so keep the last one reported
-- each day. Month-end values here are the reported truth that the
-- transaction-replay reconstruction is checked against.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    account_id TEXT NOT NULL,
    date       TEXT NOT NULL,             -- local date of the sync
    current    REAL,
    available  REAL,
    at         TEXT NOT NULL,
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS investment_transactions (
    investment_transaction_id TEXT PRIMARY KEY,
    account_id   TEXT NOT NULL,
    item_id      TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    security_id  TEXT,
    date         TEXT,
    name         TEXT,
    amount       REAL,
    quantity     REAL,
    price        REAL,
    type         TEXT,
    subtype      TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS credit_liabilities (
    account_id             TEXT PRIMARY KEY,
    item_id                TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    last_statement_balance REAL,
    last_statement_date    TEXT,
    minimum_payment        REAL,
    next_payment_due_date  TEXT,
    aprs_json              TEXT,
    updated_at             TEXT
);

-- Per item+product outcome of each sync, used by the coverage report.
CREATE TABLE IF NOT EXISTS sync_results (
    item_id    TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    product    TEXT NOT NULL,
    status     TEXT NOT NULL,        -- ok | not_supported | not_ready | error
    detail     TEXT,
    counts_json TEXT,
    at         TEXT NOT NULL,
    PRIMARY KEY (item_id, product)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(month_closes)")}
        if "balance_source" not in cols:
            self.conn.execute("ALTER TABLE month_closes ADD COLUMN balance_source TEXT")
        if "recommendation_json" not in cols:
            self.conn.execute("ALTER TABLE month_closes ADD COLUMN recommendation_json TEXT")
        if "category" not in {r[1] for r in self.conn.execute("PRAGMA table_info(txn_overrides)")}:
            self.conn.execute("ALTER TABLE txn_overrides ADD COLUMN category TEXT")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    # --- items -----------------------------------------------------------------
    def add_item(self, item_id: str, token_enc: bytes, institution_id: str | None,
                 institution_name: str | None, link_kind: str) -> None:
        ts = now_iso()
        with self.tx() as c:
            c.execute(
                """INSERT INTO items(item_id, institution_id, institution_name, link_kind,
                                     access_token_enc, created_at)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET access_token_enc=excluded.access_token_enc""",
                (item_id, institution_id, institution_name, link_kind, token_enc, ts),
            )
            c.execute(
                "INSERT OR IGNORE INTO item_ledger(item_id, institution_name, created_at) VALUES (?,?,?)",
                (item_id, institution_name, ts),
            )

    def items_ever_created(self) -> int:
        return self.one("SELECT COUNT(*) AS n FROM item_ledger")["n"]

    def set_result(self, item_id: str, product: str, status: str,
                   detail: str | None = None, counts: dict | None = None) -> None:
        self.conn.execute(
            """INSERT INTO sync_results(item_id, product, status, detail, counts_json, at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(item_id, product) DO UPDATE SET status=excluded.status,
                 detail=excluded.detail, counts_json=excluded.counts_json, at=excluded.at""",
            (item_id, product, status, detail, json.dumps(counts or {}), now_iso()),
        )

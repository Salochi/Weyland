"""Pair the two legs of money that moves between the user's own accounts.

Runs after classification, over the *whole* history (a pair can straddle a
month boundary). Sets ``match_status`` / ``match_with`` on each row:

- transfers (card_payment, internal_transfer): an outflow on one account pairs
  with an equal inflow of the same class on another account within ``window``
  days. Unpaired legs are ``unmatched`` (review) unless the other side is
  simply outside the synced history (``outside_history``) or too new to have
  posted yet (``awaiting``). Unpaired legs keep their class either way: a card
  payment is never spending, matched or not.
- refunds: a card credit pairs with an earlier purchase of the exact amount at
  the same merchant (``refunded`` on both). A fully refunded purchase nets to
  zero, so the close drops both; partial refunds stay ``unmatched`` and simply
  offset spending in their own month.

Manually overridden rows are trusted as is and never sent to review.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

TRANSFER_CLASSES = ("card_payment", "internal_transfer")
CASH = ("hysa", "checking")
REFUND_WINDOW_DAYS = 120


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _merchant_key(name: str) -> str:
    words = re.findall(r"[A-Z]{2,}", name.upper())
    return words[0] if words else ""


def _counterpart_roles(row) -> tuple[str, ...]:
    if row.cls == "internal_transfer":
        return CASH
    return CASH if row.role == "card" else ("card",)


def match(rows: list, window: int = 5, today: date | None = None) -> dict:
    today = today or date.today()
    for r in rows:
        r.match_status, r.match_with = "", None
    _pair_transfers(rows, window)
    _pair_refunds(rows)

    first_seen: dict[str, str] = {}
    for r in rows:
        if r.account_id not in first_seen or r.date < first_seen[r.account_id]:
            first_seen[r.account_id] = r.date
    roles = {r.account_id: r.role for r in rows}
    for r in rows:
        if r.cls not in TRANSFER_CLASSES or r.match_status:
            continue
        if r.overridden:
            r.match_status = "confirmed"
            continue
        starts = [first_seen[a] for a, role in roles.items()
                  if role in _counterpart_roles(r) and a != r.account_id]
        if not starts or _d(r.date) + timedelta(days=window) < _d(min(starts)):
            r.match_status = "outside_history"
        elif (today - _d(r.date)).days <= window:
            r.match_status = "awaiting"
        else:
            r.match_status = "unmatched"
    return summarize(rows)


def _pair_transfers(rows: list, window: int) -> None:
    legs = [r for r in rows if r.cls in TRANSFER_CLASSES]
    cands = []
    for i, a in enumerate(legs):
        if a.amount <= 0:
            continue  # pair from the outflow side only
        for b in legs:
            if (b.amount < 0 and b.cls == a.cls and b.account_id != a.account_id
                    and abs(a.amount + b.amount) < 0.005):
                gap = abs((_d(a.date) - _d(b.date)).days)
                if gap <= window:
                    cands.append((gap, a.date, a.transaction_id, b.transaction_id, a, b))
    used: set[str] = set()
    for *_, a, b in sorted(cands, key=lambda c: c[:4]):  # closest dates first, deterministic
        if a.transaction_id in used or b.transaction_id in used:
            continue
        used |= {a.transaction_id, b.transaction_id}
        a.match_status = b.match_status = "matched"
        a.match_with, b.match_with = b.transaction_id, a.transaction_id


def _pair_refunds(rows: list) -> None:
    buys = [r for r in rows if r.cls == "spending" and r.role == "card" and r.amount > 0]
    used: set[str] = set()
    for ref in sorted((r for r in rows if r.cls == "refund"), key=lambda r: (r.date, r.transaction_id)):
        key = _merchant_key(ref.name)
        hits = [b for b in buys if b.transaction_id not in used and key and _merchant_key(b.name) == key
                and abs(b.amount + ref.amount) < 0.005 and b.date <= ref.date
                and (_d(ref.date) - _d(b.date)).days <= REFUND_WINDOW_DAYS]
        if not hits:
            ref.match_status = "unmatched"  # partial refund; offsets its own month
            continue
        buy = max(hits, key=lambda b: (b.date, b.transaction_id))  # most recent purchase
        used.add(buy.transaction_id)
        ref.match_status = buy.match_status = "refunded"
        ref.match_with, buy.match_with = buy.transaction_id, ref.transaction_id


def needs_review(r) -> bool:
    return r.cls == "unknown" or (r.cls in TRANSFER_CLASSES and r.match_status == "unmatched")


def summarize(rows: list) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        if r.match_status:
            out[r.match_status] = out.get(r.match_status, 0) + 1
    return out

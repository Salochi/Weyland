"""Waterfall recommendation for a closed (or about-to-close) month.

Read-only: this recommends transfers, it never moves money.

    investable = HYSA at month end − floor − open earmarks − card balances owed at month end

Card balances are replayed to month end the same way as the HYSA (Plaid only
reports "now"). They are money already spent that still sits in the HYSA until
autopay or a manual payment pulls it out, so it is not investable. A card in
credit (negative owed) counts as 0.

Roth schedule for IRS tax year Y: the window is the April-Y close through the
March-(Y+1) close. For close month M in the window:

    remaining   = limit[Y] − contributions tagged Y dated on or before M's end
    closes_left = months M..March-(Y+1) not marked "skip Roth" (M included)
    installment = floor(remaining / closes_left) to whole dollars; the last
                  open close takes whatever is left, so the schedule sums to
                  exactly `remaining` and can never exceed the IRS limit

- Skipped month: Roth 0. The installment it would have had stays in the HYSA
  (held back, not sent to the brokerage) and is absorbed by later closes.
- Confirmed bonus in M: Roth target = max(installment, min(bonus, remaining)).
- Roth = min(target, investable); everything else investable → brokerage.
- A tax year without a configured limit is refused, not guessed.

The month's surplus is shown as a health metric only. It is already inside the
HYSA balance, so adding it would recommend the same money twice.
"""

from __future__ import annotations

import sqlite3
from calendar import monthrange
from datetime import date

from .classify import WaterfallConfig


def tax_year(month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    return y if m >= 4 else y - 1


def _next(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"


def window_from(month: str) -> list[str]:
    """Close months from `month` through the March that ends its tax year."""
    end = f"{tax_year(month) + 1}-03"
    out, m = [], month
    while m <= end:
        out.append(m)
        m = _next(m)
    return out


def _month_end(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, monthrange(y, m)[1]).isoformat()


def contributed(conn: sqlite3.Connection, year: int, through: str | None = None) -> float:
    q = "SELECT COALESCE(SUM(amount), 0) FROM roth_contributions WHERE tax_year = ?"
    args: list[object] = [year]
    if through:
        q += " AND date <= ?"
        args.append(through)
    return round(conn.execute(q, args).fetchone()[0], 2)


def split_installments(remaining: float, n: int) -> list[float]:
    """Split `remaining` over `n` closes: whole dollars rounded down, the last takes the rest.

    Works in cents so the parts always sum to exactly `remaining` (never over the limit).
    """
    if n <= 0:
        return []
    cents = max(0, round(remaining * 100))
    each = cents // n // 100 * 100
    return [each / 100] * (n - 1) + [(cents - each * (n - 1)) / 100]


def skipped_months(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT month FROM roth_skips")}


def recommend(conn: sqlite3.Connection, cfg: WaterfallConfig, month: str, hysa: float | None,
              cards_owed: dict[str, float], confirmed_bonus: float) -> dict:
    if hysa is None:
        return {"error": "no month-end HYSA balance"}
    earmarks = [{"name": e["name"], "amount": e["amount"], "due": e["due"]} for e in cfg.open_earmarks]
    held_earmarks = round(sum(e["amount"] for e in earmarks), 2)
    owed = round(sum(max(v, 0.0) for v in cards_owed.values()), 2)
    investable = round(max(0.0, hysa - cfg.hysa_floor - held_earmarks - owed), 2)
    rec: dict = {
        "month": month, "hysa": round(hysa, 2), "floor": cfg.hysa_floor, "earmarks": earmarks,
        "earmarks_total": held_earmarks, "cards_owed": owed, "investable": investable,
        "confirmed_bonus": round(confirmed_bonus, 2),
    }
    y = tax_year(month)
    limit = cfg.roth_limits.get(y)
    if limit is None:
        rec["error"] = f"no Roth limit configured for tax year {y} (add it to [roth_limits])"
        return rec
    skips = skipped_months(conn)
    done = contributed(conn, y, through=_month_end(month))
    remaining = round(max(0.0, limit - done), 2)
    future = [m for m in window_from(month)[1:] if m not in skips]
    is_skip = month in skips
    left = len(future) + (0 if is_skip else 1)
    installment = split_installments(remaining, left)[0] if left else 0.0
    rec["roth"] = {"tax_year": y, "limit": limit, "contributed": done, "remaining": remaining,
                   "closes_left": left, "skipped": is_skip, "installment": installment}
    if is_skip:
        # what this month would have paid had it not been skipped; it stays in the HYSA
        held = split_installments(remaining, len(future) + 1)[0]
        roth, hold = 0.0, min(held, investable)
    else:
        target = installment
        if confirmed_bonus > 0:
            target = max(installment, min(confirmed_bonus, remaining))
        roth, hold = round(min(target, investable), 2), 0.0
    rec["to_roth"] = roth
    rec["held_for_roth"] = round(hold, 2)
    rec["to_brokerage"] = round(investable - roth - hold, 2)
    rec["shortfall"] = round(max(0.0, (installment if not is_skip else 0) - roth), 2)
    return rec

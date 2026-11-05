"""Demo mode: the real UI on made-up data.

``weyland demo`` builds a throwaway database for a fictional person, dated
relative to today so it never goes stale, and serves the normal app on it.
No Plaid keys, no network calls, nothing read from or written to /etc.
Every institution, name, amount and account number here is invented.

The data is built to pass the same checks as real data: older months are
closed through ``close_month`` (balance replay, card checks, transfer
pairing), so if a change breaks the close logic, building the demo fails too.
"""

from __future__ import annotations

import json
import random
import tempfile
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .crypto import TokenBox, create_key
from .db import DB, now_iso

HISTORY_MONTHS = 5          # full months before the current one; all but the last get closed
DEMO_LOGIN = "demo@example.com"
DEFAULT_PORT = 8731         # next to a real install's 8730, so both can run on one box

ITEMS = (  # item_id, institution, link kind, consent days left (None = credential login)
    ("demo-harbor", "Harbor Bank", "bank", 250),
    ("demo-summit", "Summit Credit Union", "bank", None),
    ("demo-cedar", "Cedar Brokerage", "investment", 300),
    ("demo-northwind", "Northwind Retirement", "investment", 19),  # shows the "expiring" state
)
ACCOUNTS = (  # account_id, item, name, mask, type, subtype, plaid credit limit
    ("chk", "demo-harbor", "Everyday Checking", "4417", "depository", "checking", None),
    ("hysa", "demo-harbor", "High Yield Savings", "8832", "depository", "savings", None),
    ("hcard", "demo-harbor", "Harbor Rewards Card", "2209", "credit", "credit card", 8000.0),
    ("scard", "demo-summit", "Cash Back Visa", "5561", "credit", "credit card", None),
    ("ssav", "demo-summit", "Share Savings", "0001", "depository", "savings", None),
    ("brk", "demo-cedar", "Individual Brokerage", "7710", "investment", "brokerage", None),
    ("roth", "demo-cedar", "Roth IRA", "7724", "investment", "roth", None),
    ("k401", "demo-northwind", "Acme Robotics 401(k)", None, "investment", "401k", None),
)
ITEM_OF = {a[0]: a[1] for a in ACCOUNTS}
OPENING = {"chk": 2400.00, "hysa": 21500.00, "ssav": 5.12, "hcard": 0.0, "scard": 0.0}

PAYCHECK = 2412.38
BONUS = 6850.00
RENT, RENT_SHARE = 1650.00, 825.00
ROTH_MONTHLY, BROKERAGE_MONTHLY = 625.00, 1000.00

SECURITIES = (  # security_id, ticker, name, type, price
    ("sec-vti", "VTI", "Vanguard Total Stock Market ETF", "etf", 289.40),
    ("sec-vxus", "VXUS", "Vanguard Total International Stock ETF", "etf", 66.10),
    ("sec-bnd", "BND", "Vanguard Total Bond Market ETF", "etf", 72.80),
    ("sec-vt", "VT", "Vanguard Total World Stock ETF", "etf", 121.30),
    ("sec-tdf", None, "Northwind Target Retirement 2060 Fund", "mutual fund", 24.87),
    ("sec-cash", None, "Cash & sweep", "cash", 1.0),
)
HOLDINGS = (  # account, security, quantity, cost basis
    ("brk", "sec-vti", 38.512, 9840.00),
    ("brk", "sec-vxus", 52.0, 3310.00),
    ("brk", "sec-bnd", 20.0, 1495.00),
    ("brk", "sec-cash", 312.55, None),
    ("roth", "sec-vt", 61.204, 6800.00),
    ("roth", "sec-cash", 18.40, None),
    ("k401", "sec-tdf", 1412.30, 31120.00),
)
# Amazon prices come from a fixed list so the partial refund below never pairs by accident.
AMAZON_PRICES = (14.99, 18.49, 27.35, 32.99, 41.20, 56.78, 64.99, 89.95, 112.40)
AMAZON_RETURN = 23.97


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _ym(d: date) -> str:
    return d.isoformat()[:7]


def _tax_year(ym: str) -> int:
    return int(ym[:4]) if int(ym[5:7]) >= 4 else int(ym[:4]) - 1


class _Gen:
    """Collects transactions; anything dated after today is silently dropped."""

    def __init__(self, today: date, rng: random.Random):
        self.today, self.rng, self.rows, self.n = today, rng, [], 0

    def tx(self, acct: str, d: date, amount: float, name: str, prim: str, det: str = "",
           merchant: str | None = None, pending: bool = False) -> str | None:
        if d > self.today:
            return None
        self.n += 1
        tid = f"demo-tx-{self.n:05d}"
        self.rows.append({"transaction_id": tid, "account_id": acct, "item_id": ITEM_OF[acct],
                          "date": d.isoformat(), "amount": round(amount, 2), "name": name,
                          "merchant_name": merchant, "pending": 1 if pending else 0,
                          "category_primary": prim, "category_detail": det})
        return tid

    def buy(self, acct: str, d: date, amount: float, name: str, prim: str, det: str,
            merchant: str | None = None) -> None:
        # The newest card purchases are still pending, like a real feed.
        self.tx(acct, d, amount, name, prim, det, merchant, pending=(self.today - d).days <= 1)

    def net(self, acct: str, ym: str) -> float:
        """A card's statement for the month: posted charges and refunds, never payments."""
        return round(sum(r["amount"] for r in self.rows
                         if r["account_id"] == acct and r["date"][:7] == ym and not r["pending"]
                         and r["category_primary"] != "LOAN_PAYMENTS"), 2)


def _generate(today: date, months: list[date], rng: random.Random) -> tuple[_Gen, dict]:
    g = _Gen(today, rng)
    marks: dict = {"roth": []}
    u = rng.uniform

    # Paychecks: every other Friday into the HYSA.
    d = months[0] + timedelta(days=(4 - months[0].weekday()) % 7)
    while d <= today:
        g.tx("hysa", d, -PAYCHECK, "ACME ROBOTICS PAYROLL", "INCOME", "INCOME_WAGES", "Acme Robotics")
        d += timedelta(days=14)

    for i, mf in enumerate(months):
        y, m = mf.year, mf.month
        last = monthrange(y, m)[1]

        def day(n: int) -> date:
            return date(y, m, min(max(n, 1), last))

        # Fixed costs: rent from checking, the roommate's half back by Zelle.
        g.tx("chk", day(1), RENT, "OAKWOOD APARTMENTS RENT", "RENT_AND_UTILITIES",
             "RENT_AND_UTILITIES_RENT", "Oakwood Apartments")
        g.tx("chk", day(1), -RENT_SHARE, "ZELLE FROM JORDAN M", "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
        g.tx("chk", day(15), 112.40, "GEICO AUTO INSURANCE", "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE",
             "GEICO")
        g.tx("scard", day(9), round(u(64, 118), 2), "CITY POWER & LIGHT", "RENT_AND_UTILITIES",
             "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "City Power & Light")
        g.tx("scard", day(14), 70.00, "XFINITY INTERNET", "RENT_AND_UTILITIES",
             "RENT_AND_UTILITIES_INTERNET_AND_CABLE", "Xfinity")
        # Subscriptions on the rewards card.
        g.tx("hcard", day(3), 11.99, "SPOTIFY USA", "ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO", "Spotify")
        g.tx("hcard", day(12), 15.49, "NETFLIX.COM", "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES", "Netflix")
        g.tx("hcard", day(18), 2.99, "APPLE.COM/BILL", "GENERAL_SERVICES",
             "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Apple")
        if date(y, m, last) < today:
            g.tx("hysa", day(last), -round(u(64, 82), 2), "MONTHLY INTEREST PAID", "INCOME",
                 "INCOME_INTEREST_EARNED")

        # Everyday spending.
        d = day(2)
        while d.month == m:
            store = rng.choice(("TRADER JOE'S", "WHOLE FOODS MARKET", "SAFEWAY"))
            g.buy("hcard", d, round(u(38, 140), 2), f"{store} #{rng.randint(100, 899)}", "FOOD_AND_DRINK",
                  "FOOD_AND_DRINK_GROCERIES", store.title())
            d += timedelta(days=rng.randint(5, 8))
        for _ in range(rng.randint(8, 12)):
            place = rng.choice(("CHIPOTLE", "SHAKE SHACK", "PANERA BREAD", "THAI BASIL KITCHEN", "PHO SAIGON"))
            g.buy("hcard", day(rng.randint(1, last)), round(u(9, 58), 2), place, "FOOD_AND_DRINK",
                  "FOOD_AND_DRINK_RESTAURANT", place.title())
        for _ in range(rng.randint(4, 8)):
            g.buy("hcard", day(rng.randint(1, last)), round(u(4.5, 9), 2), "BLUE DOOR COFFEE", "FOOD_AND_DRINK",
                  "FOOD_AND_DRINK_COFFEE", "Blue Door Coffee")
        for n in (1, rng.randint(9, 13), rng.randint(19, 24)):  # day 1: Summit's history starts with the month
            g.buy("scard", day(n), round(u(38, 55), 2), "SHELL OIL 5741", "TRANSPORTATION", "TRANSPORTATION_GAS",
                  "Shell")
        for _ in range(rng.randint(3, 4)):
            g.buy("scard", day(rng.randint(1, last)), rng.choice(AMAZON_PRICES), "AMAZON.COM*MK2 SEATTLE",
                  "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "Amazon")
        for _ in range(rng.randint(1, 2)):
            g.buy("scard", day(rng.randint(1, last)), round(u(18, 95), 2), "TARGET T-1123", "GENERAL_MERCHANDISE",
                  "GENERAL_MERCHANDISE_SUPERSTORES", "Target")
        for _ in range(rng.randint(2, 4)):
            g.buy("scard", day(rng.randint(1, last)), round(u(11, 34), 2), "UBER *TRIP", "TRANSPORTATION",
                  "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "Uber")
        if rng.random() < 0.6:
            g.buy("hcard", day(rng.randint(1, last)), round(u(24, 48), 2), "AMC THEATRES 0412", "ENTERTAINMENT",
                  "ENTERTAINMENT_TV_AND_MOVIES", "AMC Theatres")

        # One-off stories, placed by month.
        back = len(months) - 1 - i  # 0 = current month
        if back == 0:  # a full refund, recent enough to sit near the top of the Spending list
            d0 = max(mf, today - timedelta(days=6))
            g.tx("hcard", d0, 129.95, "REI #0142 SEATTLE", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_SPORTING_GOODS", "REI")
            g.tx("hcard", max(d0, today - timedelta(days=2)), -129.95, "REI #0142 RETURN", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_SPORTING_GOODS", "REI")
        if back == 3:  # a bonus (confirmed below) and a cash-back redemption
            marks["bonus"] = g.tx("hysa", day(15), -BONUS, "ACME ROBOTICS PAYROLL", "INCOME", "INCOME_WAGES",
                                  "Acme Robotics")
            g.tx("hcard", day(10), -25.00, "REWARDS REDEMPTION CREDIT", "GENERAL_SERVICES", "")
        if back == 0:  # a partial refund: offsets its own month
            g.tx("scard", max(mf, today - timedelta(days=3)), -AMAZON_RETURN, "AMAZON.COM RETURN", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "Amazon")
        if back == 0:  # two deposits the rules can't place: they land in the review queue
            d = max(mf, today - timedelta(days=1))
            g.tx("chk", d, -140.00, "VENMO CASHOUT", "TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
            g.tx("chk", d, -300.00, "MOBILE CHECK DEPOSIT", "TRANSFER_IN", "TRANSFER_IN_DEPOSIT")

    # Money movement, after purchases exist: last month's card bills, the transfers that
    # fund them, rent money for next month, and the monthly Roth/brokerage contributions.
    for prev, mf in zip(months, months[1:]):
        y, m = mf.year, mf.month
        h_due, s_due = g.net("hcard", _ym(prev)), g.net("scard", _ym(prev))
        g.tx("hysa", date(y, m, 20), h_due + s_due, "Withdrawal to Checking", "TRANSFER_OUT",
             "TRANSFER_OUT_ACCOUNT_TRANSFER")
        g.tx("chk", date(y, m, 20), -(h_due + s_due), "Deposit from Savings", "TRANSFER_IN",
             "TRANSFER_IN_ACCOUNT_TRANSFER")
        g.tx("chk", date(y, m, 22), h_due, "HARBOR BANK CARD PMT", "LOAN_PAYMENTS",
             "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        g.tx("hcard", date(y, m, 22), -h_due, "PAYMENT - THANK YOU", "LOAN_PAYMENTS",
             "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        g.tx("chk", date(y, m, 26), s_due, "SUMMIT CU ACH PMT", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
        g.tx("scard", date(y, m, 26), -s_due, "ACH PAYMENT RECEIVED", "LOAN_PAYMENTS",
             "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        g.tx("hysa", date(y, m, 28), RENT - RENT_SHARE, "Withdrawal to Checking", "TRANSFER_OUT",
             "TRANSFER_OUT_ACCOUNT_TRANSFER")
        g.tx("chk", date(y, m, 28), -(RENT - RENT_SHARE), "Deposit from Savings", "TRANSFER_IN",
             "TRANSFER_IN_ACCOUNT_TRANSFER")
        if g.tx("hysa", date(y, m, 6), ROTH_MONTHLY, "CEDAR BROKERAGE MONEYLINK ROTH", "TRANSFER_OUT",
                "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS"):
            marks["roth"].append((date(y, m, 6), ROTH_MONTHLY, _tax_year(_ym(prev))))
            g.tx("hysa", date(y, m, 6), BROKERAGE_MONTHLY, "CEDAR BROKERAGE MONEYLINK INDIV", "TRANSFER_OUT",
                 "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS")
    return g, marks


def _waterfall_toml(start: date, today: date) -> str:
    years = range(start.year - 1, today.year + 2)
    limits = "\n".join(f"{y} = {7000 if y <= 2025 else 7500}" for y in years)
    return f"""# Demo config: a fictional person. See deploy/waterfall.toml.example for the real thing.
[accounts]
hysa       = "Harbor Bank:8832"
checking   = "Harbor Bank:4417"
card       = ["Harbor Bank:2209", "Summit Credit Union:5561"]
brokerage  = "Cedar Brokerage:7710"
roth       = "Cedar Brokerage:7724"
retirement = "Northwind Retirement:Acme Robotics 401(k)"
ignore     = ["Summit Credit Union:0001"]

[rules]
employer     = ['^ACME ROBOTICS PAYROLL']
reimbursers  = ['^ZELLE FROM JORDAN']
card_payment = ['^SUMMIT CU ACH']
investment   = ['^CEDAR BROKERAGE']

[waterfall]
hysa_floor       = 15000
bonus_multiple   = 1.5
close_grace_days = 3
start            = "{_ym(start)}"

[[earmark]]
name   = "vacation"
amount = 1200.00
status = "open"
due    = "{_ym(_add_months(today, 2))}"
note   = "flights booked; hotel due at check-in"

[categories]
rent          = ['(?i)^OAKWOOD APARTMENTS']
subscriptions = ['(?i)^SPOTIFY', '(?i)^NETFLIX', '(?i)^APPLE\\.COM/BILL']

[cards]
limits     = {{ "Summit Credit Union:5561" = 6000 }}
manual_pay = ["Summit Credit Union:5561"]

[rewards]
redemptions = []

[[rewards.card]]
card  = "Harbor Bank:2209"
base  = 0.01

[[rewards.card.bonus]]
label = "groceries"
rate  = 0.03
plaid = ["FOOD_AND_DRINK_GROCERIES"]

[[rewards.card.bonus]]
label = "dining"
rate  = 0.03
plaid = ["FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK_FAST_FOOD", "FOOD_AND_DRINK_COFFEE"]

[[rewards.card]]
card          = "Summit Credit Union:5561"
base          = 0.015
balance       = 42.10
balance_as_of = "{start.isoformat()}"

[roth_limits]
{limits}
"""


def _write(db: DB, box: TokenBox, g: _Gen, today: date, start: date) -> None:
    c = db.conn
    now = datetime.now(timezone.utc).replace(microsecond=0)
    for item_id, inst, kind, consent_days in ITEMS:
        db.add_item(item_id, box.seal("demo-no-token"), f"ins_{item_id}", inst, kind)
        meta = {"billed_products": ["transactions"] if kind == "bank" else ["investments"],
                "consent_expiration_time": ((now + timedelta(days=consent_days, hours=12)).isoformat().replace("+00:00", "Z")
                                            if consent_days is not None else None),
                "item_error": None}
        c.execute("UPDATE items SET created_at=?, last_sync_at=?, products_json=? WHERE item_id=?",
                  (f"{start.isoformat()}T12:00:00+00:00", (now - timedelta(hours=3)).isoformat(),
                   json.dumps(meta), item_id))
        products = ("accounts", "transactions", "liabilities") if kind == "bank" else \
                   ("accounts", "transactions", "investments", "investment_transactions")
        for p in products:
            if item_id == "demo-northwind" and p == "transactions":
                db.set_result(item_id, p, "not_supported", "PRODUCTS_NOT_SUPPORTED")
            else:
                db.set_result(item_id, p, "ok", None, {"update_status": "HISTORICAL_UPDATE_COMPLETE"}
                              if p == "transactions" else {})
    for r in g.rows:
        c.execute("INSERT INTO transactions(transaction_id, account_id, item_id, date, amount, iso_currency, "
                  "name, merchant_name, pending, category_primary, category_detail, updated_at) "
                  "VALUES (:transaction_id, :account_id, :item_id, :date, :amount, 'USD', :name, :merchant_name, "
                  ":pending, :category_primary, :category_detail, '')", r)

    # Balances: current = opening + every posted movement; cards owe what was spent and not paid.
    posted = [r for r in g.rows if not r["pending"]]
    current: dict[str, float] = {}
    for aid, *_ in ACCOUNTS:
        moved = sum(r["amount"] for r in posted if r["account_id"] == aid)
        if aid in OPENING:
            current[aid] = round(OPENING[aid] + (moved if aid in ("hcard", "scard") else -moved), 2)
    prices = {s[0]: s[4] for s in SECURITIES}
    for aid in ("brk", "roth", "k401"):
        current[aid] = round(sum(q * prices[s] for a, s, q, _ in HOLDINGS if a == aid), 2)
    for aid, item, name, mask, typ, sub, lim in ACCOUNTS:
        c.execute("INSERT INTO accounts(account_id, item_id, name, official_name, mask, type, subtype, current, "
                  "available, credit_limit, iso_currency, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'USD', ?)",
                  (aid, item, name, name, mask, typ, sub, current[aid],
                   current[aid] if typ == "depository" else None, lim, now_iso()))

    # Daily snapshots, replayed backwards from today exactly like close.py does, so the
    # balance checks pass. Investments get a gentle random walk ending at today's value.
    rng = random.Random(f"weyland-demo-markets-{today.isoformat()}")
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    walk = {aid: [1.0] for aid in ("brk", "roth", "k401")}
    for aid in walk:
        for _ in days[:-1]:
            walk[aid].append(walk[aid][-1] / (1 + rng.gauss(0.0003, 0.007)))
        walk[aid].reverse()
    contrib = {"brk": BROKERAGE_MONTHLY, "roth": ROTH_MONTHLY}
    for di, d in enumerate(days):
        ds = d.isoformat()
        for aid, *_ in ACCOUNTS:
            if aid in walk:
                later = sum(1 for x in days[di + 1:] if x.day == 6) * contrib.get(aid, 0.0)
                if aid == "k401":
                    later = sum(1 for x in days[di + 1:] if x.weekday() == 4) * 145.0
                bal = (current[aid] - later) * walk[aid][di]
            else:
                later = sum(r["amount"] for r in posted if r["account_id"] == aid and r["date"] > ds)
                bal = current[aid] - later if aid in ("hcard", "scard") else current[aid] + later
            c.execute("INSERT INTO balance_snapshots(account_id, date, current, available, at) VALUES (?,?,?,?,?)",
                      (aid, ds, round(bal, 2), None, f"{ds}T11:15:00+00:00"))

    for sid, ticker, name, typ, price in SECURITIES:
        c.execute("INSERT INTO securities(security_id, ticker, name, type, close_price, updated_at) "
                  "VALUES (?,?,?,?,?,?)", (sid, ticker, name, typ, price, now_iso()))
    for aid, sid, qty, basis in HOLDINGS:
        c.execute("INSERT INTO holdings(account_id, security_id, item_id, quantity, institution_price, "
                  "institution_value, cost_basis, price_as_of, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (aid, sid, ITEM_OF[aid], qty, prices[sid], round(qty * prices[sid], 2), basis,
                   today.isoformat(), now_iso()))
    for d in (x for x in days if x.day == 6 and x > start + timedelta(days=27)):
        for aid, sid, amt in (("brk", "sec-vti", BROKERAGE_MONTHLY), ("roth", "sec-vt", ROTH_MONTHLY)):
            c.execute("INSERT INTO investment_transactions(investment_transaction_id, account_id, item_id, "
                      "security_id, date, name, amount, quantity, price, type, subtype, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?, 'buy', 'buy', ?)",
                      (f"demo-inv-{aid}-{d.isoformat()}", aid, ITEM_OF[aid], sid, d.isoformat(),
                       f"BUY {sid[4:].upper()}", amt, round(amt / prices[sid], 4), prices[sid], now_iso()))

    # Card statements: last month's bill, due on the 22nd (Harbor, autopay) or 26th (Summit, manual).
    prev = _ym(_add_months(date(today.year, today.month, 1), -1))
    stmt_date = (date(today.year, today.month, 1) - timedelta(days=1)).isoformat()
    for aid, due_day in (("hcard", 22), ("scard", 26)):
        bill = g.net(aid, prev)
        if today.day <= due_day:
            due, owed = date(today.year, today.month, due_day), bill
        else:
            nxt = _add_months(date(today.year, today.month, 1), 1)
            due, owed = date(nxt.year, nxt.month, due_day), 0.0
        c.execute("INSERT INTO credit_liabilities(account_id, item_id, last_statement_balance, last_statement_date, "
                  "minimum_payment, next_payment_due_date, aprs_json, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (aid, ITEM_OF[aid], owed, stmt_date, round(max(25.0, owed * 0.02), 2) if owed else 0.0,
                   due.isoformat(), json.dumps([{"apr_type": "purchase_apr", "apr_percentage": 24.99}]),
                   now_iso()))


def build(dirpath: Path, today: date | None = None) -> tuple[Settings, Path]:
    """(Re)build the demo database and config in ``dirpath``; returns (settings, waterfall path)."""
    from .classify import load_config
    from .close import close_month

    today = today or date.today()
    dirpath.mkdir(parents=True, exist_ok=True)
    db_path = dirpath / "weyland-demo.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    key = dirpath / "token.key"
    create_key(key)
    settings = Settings(plaid_env="sandbox", client_id="demo", secret="demo", db_path=db_path,
                        key_path=key, allowed_users=frozenset())

    first = date(today.year, today.month, 1)
    months = [_add_months(first, -n) for n in range(HISTORY_MONTHS, -1, -1)]
    g, marks = _generate(today, months, random.Random(f"weyland-demo-{today.isoformat()}"))
    db = DB(db_path)
    _write(db, TokenBox(key), g, today, months[0])
    for d, amount, ty in marks["roth"]:
        db.conn.execute("INSERT INTO roth_contributions(date, amount, tax_year, note, at) VALUES (?,?,?,?,?)",
                        (d.isoformat(), amount, ty, "monthly installment", now_iso()))
    if marks.get("bonus"):
        db.conn.execute("INSERT INTO bonus_decisions(transaction_id, decision, at) VALUES (?, 'bonus', ?)",
                        (marks["bonus"], now_iso()))

    wf = dirpath / "waterfall.toml"
    wf.write_text(_waterfall_toml(months[0], today))
    cfg = load_config(wf)
    for mf in months[:-2]:  # last month stays open ("ready to close"), this month is in progress
        close_month(db.conn, cfg, _ym(mf), today)
    db.conn.close()
    return settings, wf


def app(dirpath: Path | None = None, today: date | None = None):
    from .web import create_app
    dirpath = dirpath or Path(tempfile.mkdtemp(prefix="weyland-demo-"))
    settings, wf = build(dirpath, today)
    return create_app(settings, waterfall_path=wf, demo=True)


def serve(host: str, port: int, dirpath: str | None) -> int:
    import uvicorn
    path = Path(dirpath) if dirpath else Path(tempfile.mkdtemp(prefix="weyland-demo-"))
    application = app(path)
    print(f"Weyland demo (made-up data in {path}) on http://{host}:{port}/", flush=True)
    uvicorn.run(application, host=host, port=port, log_level="warning")
    return 0

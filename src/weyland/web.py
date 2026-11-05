"""Web UI: Plaid Link page + coverage view.

Binds to 127.0.0.1 only. The sole way in is `tailscale serve`, which proxies
tailnet HTTPS to localhost and injects `Tailscale-User-Login` for the
authenticated tailnet user. Because nothing else can reach the loopback port,
that header is trustworthy here and is checked against WEYLAND_ALLOWED_USERS.
"""

from __future__ import annotations

import hashlib
import html
import json
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from . import report
from .config import ConfigError, Settings, load_settings
from .crypto import TokenBox
from .db import DB
from .plaid_client import PlaidClient, PlaidError
from .sync import Syncer

# Trial plan: 10 Items, ever. Stop new real links before the cap so a slot is
# always left for re-linking an institution that breaks.
PRODUCTION_ITEM_GUARD = 8

LINK_KINDS = {
    # Banks and cards. Liabilities is optional so plain checking/savings still link.
    "bank": {"products": ["transactions"], "optional_products": ["liabilities"]},
    # Brokerage, IRA, 401k. Transactions optional: many 401k custodians lack it.
    "investment": {"products": ["investments"], "optional_products": ["transactions"]},
}


def _unconfigured_app(reason: str) -> FastAPI:
    """Served until Plaid keys exist: setup instructions, 503 on every /api route."""
    app = FastAPI(title="Weyland", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return f"unconfigured: {reason}"

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (
            "<!doctype html><html><head><title>Weyland</title></head>"
            "<body style='font-family:monospace;background:#16171a;color:#ddd;"
            "padding:2rem;line-height:1.7;max-width:40rem'>"
            "<h2 style='color:#f0a868'>Weyland</h2>"
            "<p>Weyland is installed but not configured yet:</p>"
            "<p>1. Put your Plaid keys in <code>/etc/weyland/plaid.env</code> "
            "(<code>PLAID_CLIENT_ID</code> plus <code>PLAID_SECRET_SANDBOX</code> "
            "or <code>PLAID_SECRET_PRODUCTION</code>).</p>"
            "<p>2. Create <code>/etc/weyland/waterfall.toml</code> from "
            "<code>deploy/waterfall.toml.example</code>.</p>"
            "<p>3. <code>systemctl restart weyland-web</code>.</p>"
            f"<p style='color:#999'>{html.escape(reason)}</p></body></html>"
        )

    @app.api_route("/api/{rest:path}", methods=["GET", "POST"])
    def unconfigured(rest: str) -> JSONResponse:
        return JSONResponse({"error": "unconfigured", "message": reason}, status_code=503)

    return app


def create_app(settings: Settings | None = None, plaid: PlaidClient | None = None,
               waterfall_path: Path | None = None, demo: bool = False) -> FastAPI:
    if settings is None:
        # A fresh install has no /etc/weyland/plaid.env yet. Boot into an
        # "unconfigured" app that says exactly that, instead of crash-looping
        # the unit from the moment install.sh enables it.
        try:
            settings = load_settings()
        except ConfigError as e:
            return _unconfigured_app(str(e))
    db = DB(settings.db_path)
    box = TokenBox(settings.key_path)
    plaid = plaid or PlaidClient(settings)
    syncer = Syncer(db, plaid, box)
    # One sqlite connection is shared by every route, and FastAPI runs sync routes on a
    # thread pool, so the page's parallel first-load calls raced on it (InterfaceError,
    # IndexError from a half-read PRAGMA). Single user: route every DB touch through
    # one lock. A dependency (not middleware) so it runs in the route's own thread.
    db_lock = threading.Lock()

    def serialised():
        with db_lock:
            yield

    app = FastAPI(title="Weyland", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(serialised)])

    def user(request: Request) -> str:
        if demo:
            # Demo mode serves made-up data (weyland.demo) to whoever asks.
            from .demo import DEMO_LOGIN
            return DEMO_LOGIN
        login = (request.headers.get("tailscale-user-login") or "").lower()
        if not login or (settings.allowed_users and login not in settings.allowed_users):
            raise HTTPException(403, "tailnet user not allowed")
        return login

    def same_origin(request: Request) -> None:
        # Blocks cross-site form posts; our own fetch() calls send this header.
        if request.headers.get("x-weyland") != "1":
            raise HTTPException(400, "missing x-weyland header")

    def live(_: Request) -> None:
        # Routes that would call Plaid. The demo has no keys and makes no network calls.
        if demo:
            raise HTTPException(403, "demo mode: connecting and syncing banks is disabled")

    @app.exception_handler(PlaidError)
    async def plaid_error(_req: Request, err: PlaidError):
        return JSONResponse(
            {"error": f"{err.error_type}/{err.error_code}", "message": err.display_message or err.error_message,
             "request_id": err.request_id},
            status_code=502,
        )

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/", response_class=HTMLResponse)
    def index(login: str = Depends(user)) -> str:
        page = (Path(__file__).parent / "static" / "index.html").read_text()
        return (page.replace("{{ENV}}", "demo" if demo else html.escape(settings.plaid_env))
                    .replace("{{USER}}", html.escape(login)))

    @app.get("/api/state")
    def state(_: str = Depends(user)) -> dict:
        rep = report.build(db)
        return {
            "plaid_env": "demo" if demo else settings.plaid_env,
            "items_ever_created": rep["items_ever_created"],
            "item_guard": PRODUCTION_ITEM_GUARD if settings.is_production else None,
            "report": rep,
            "report_md": report.to_markdown(rep, settings.plaid_env),
        }

    @app.post("/api/link-token")
    async def link_token(request: Request, login: str = Depends(user), _=Depends(same_origin),
                         __=Depends(live)) -> dict:
        body = await request.json()
        kind = body.get("kind")
        if kind not in LINK_KINDS:
            raise HTTPException(400, f"kind must be one of {sorted(LINK_KINDS)}")
        if settings.is_production:
            used = db.items_ever_created()
            if used >= PRODUCTION_ITEM_GUARD and not body.get("override_guard"):
                raise HTTPException(409, f"{used} Items already created; guard is {PRODUCTION_ITEM_GUARD}")
        req = {
            "client_name": "Weyland",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": hashlib.sha256(f"weyland:{login}".encode()).hexdigest()[:32]},
            "transactions": {"days_requested": settings.days_requested},
            **LINK_KINDS[kind],
        }
        if settings.redirect_uri:
            req["redirect_uri"] = settings.redirect_uri
        data = plaid.call("/link/token/create", **req)
        return {"link_token": data["link_token"], "expiration": data.get("expiration")}

    @app.post("/api/exchange")
    async def exchange(request: Request, _: str = Depends(user), __=Depends(same_origin),
                       ___=Depends(live)) -> dict:
        body = await request.json()
        public_token = body.get("public_token")
        if not public_token:
            raise HTTPException(400, "public_token required")
        inst = body.get("institution") or {}
        kind = body.get("kind") if body.get("kind") in LINK_KINDS else "unknown"
        # A second login at an already-connected institution is almost always a
        # mistake: the repair path for a broken login is the item's update-mode
        # Re-connect. Refusing here, before the exchange, means the Item Plaid
        # created at Link success is never stored or synced (un-exchanged
        # public tokens are discarded by Plaid), so no duplicate Item remains.
        inst_id = inst.get("institution_id")
        if inst_id:
            dup = db.one("SELECT institution_name AS n FROM items WHERE institution_id=?", inst_id)
        elif inst.get("name"):
            dup = db.one("SELECT institution_name AS n FROM items WHERE institution_name=?", inst["name"])
        else:
            dup = None
        if dup and not body.get("override_duplicate"):
            raise HTTPException(
                409, f"{dup['n']} is already connected; use its Re-connect button "
                     "instead of linking a second login")
        data = plaid.call("/item/public_token/exchange", public_token=public_token)
        db.add_item(data["item_id"], box.seal(data["access_token"]),
                    inst.get("institution_id"), inst.get("name"), kind)
        results = syncer.sync_item(data["item_id"])
        return {"item_id": data["item_id"], "results": results}

    @app.post("/api/sync")
    def sync(_: str = Depends(user), __=Depends(same_origin), ___=Depends(live)) -> dict:
        return {"results": syncer.sync_all()}

    # --- update mode (re-connect) ---------------------------------------------
    # Update mode re-authorizes an EXISTING Item: same item_id, same access
    # token, and it does NOT consume a Trial slot. Removing an Item and linking
    # it again would burn a slot, so this is the only sanctioned repair path.
    def _item_or_404(item_id: str):
        row = db.one("SELECT * FROM items WHERE item_id=?", item_id)
        if row is None:
            raise HTTPException(404, "unknown item")
        return row

    @app.post("/api/items/{item_id}/update-token")
    async def update_token(item_id: str, request: Request, login: str = Depends(user),
                           _=Depends(same_origin), __=Depends(live)) -> dict:
        row = _item_or_404(item_id)
        body = await request.json()
        req = {
            "client_name": "Weyland",
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": hashlib.sha256(f"weyland:{login}".encode()).hexdigest()[:32]},
            "access_token": box.open(row["access_token_enc"]),
        }
        if body.get("accounts"):
            # Lets the user add or remove accounts shared from this login.
            req["update"] = {"account_selection_enabled": True}
        if settings.redirect_uri:
            req["redirect_uri"] = settings.redirect_uri
        data = plaid.call("/link/token/create", **req)
        return {"link_token": data["link_token"], "expiration": data.get("expiration")}

    @app.post("/api/items/{item_id}/reconnected")
    def reconnected(item_id: str, _: str = Depends(user), __=Depends(same_origin),
                    ___=Depends(live)) -> dict:
        _item_or_404(item_id)
        # Access token is unchanged in update mode; just re-sync to clear errors
        # and pick up the new consent expiration date.
        return {"item_id": item_id, "results": syncer.sync_item(item_id)}

    # --- review queue ---------------------------------------------------------
    # Amounts are shown here on purpose: this page is tailnet-only and is the one
    # place the user reads their own numbers. They never go into report.md.
    def _classified(month: str | None = None):
        from .classify import WaterfallConfigError, classify, load_config
        try:
            return classify(db.conn, load_config(waterfall_path), month)
        except WaterfallConfigError as e:
            raise HTTPException(503, f"waterfall config: {e}") from e

    @app.get("/api/review")
    def review(_: str = Depends(user)) -> dict:
        from .classify import CLASSES, summarize
        from .match import needs_review
        rows = _classified()
        names = {r["account_id"]: f"{r['institution_name']} {r['name']}" for r in db.conn.execute(
            "SELECT a.account_id, a.name, i.institution_name FROM accounts a JOIN items i USING(item_id)")}
        queue = [{"transaction_id": r.transaction_id, "date": r.date, "account": names.get(r.account_id, r.role),
                  "role": r.role, "amount": r.amount, "name": r.name, "class": r.cls, "reason": r.reason,
                  "match_status": r.match_status} for r in rows if needs_review(r)]
        return {"summary": summarize(rows), "queue": queue,
                "classes": [c for c in CLASSES if c != "unknown"]}

    @app.post("/api/review/{transaction_id}")
    async def review_set(transaction_id: str, request: Request, _: str = Depends(user),
                         __=Depends(same_origin)) -> dict:
        from .classify import WaterfallConfigError, set_override
        body = await request.json()
        try:
            set_override(db.conn, transaction_id, body.get("class"), str(body.get("note") or "")[:200],
                         (str(body.get("category") or "")[:40] or None))
        except WaterfallConfigError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    # --- monthly close --------------------------------------------------------
    def _cfg():
        from .classify import WaterfallConfigError, load_config
        try:
            return load_config(waterfall_path)
        except WaterfallConfigError as e:
            raise HTTPException(503, f"waterfall config: {e}") from e

    @app.get("/api/months")
    def months_list(_: str = Depends(user)) -> dict:
        from .classify import WaterfallConfigError
        from .close import months
        try:
            return {"months": months(db.conn, _cfg())}
        except WaterfallConfigError as e:
            raise HTTPException(503, f"waterfall config: {e}") from e

    @app.post("/api/months/{month}/close")
    def month_close(month: str, _: str = Depends(user), __=Depends(same_origin)) -> dict:
        from .classify import WaterfallConfigError
        from .close import CloseError, close_month
        try:
            return close_month(db.conn, _cfg(), month)
        except CloseError as e:
            raise HTTPException(409, str(e)) from e
        except WaterfallConfigError as e:
            raise HTTPException(503, f"waterfall config: {e}") from e

    @app.post("/api/months/{month}/reopen")
    def month_reopen(month: str, _: str = Depends(user), __=Depends(same_origin)) -> dict:
        from .close import CloseError, reopen_month
        try:
            reopen_month(db.conn, month)
        except CloseError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    # --- waterfall bookkeeping ------------------------------------------------
    @app.get("/api/roth")
    def roth_list(_: str = Depends(user)) -> dict:
        from .waterfall import contributed, skipped_months
        cfg = _cfg()
        rows = [dict(r) for r in db.conn.execute(
            "SELECT id, date, amount, tax_year, note FROM roth_contributions ORDER BY date DESC, id DESC")]
        years = sorted({r["tax_year"] for r in rows} | set(cfg.roth_limits), reverse=True)
        return {"contributions": rows, "skips": sorted(skipped_months(db.conn)),
                "years": [{"tax_year": y, "limit": cfg.roth_limits.get(y), "contributed": contributed(db.conn, y)}
                          for y in years]}

    @app.post("/api/roth")
    async def roth_add(request: Request, _: str = Depends(user), __=Depends(same_origin)) -> dict:
        from datetime import date as _date
        from .db import now_iso
        from .waterfall import tax_year
        b = await request.json()
        try:
            d = _date.fromisoformat(str(b.get("date")))
            amount = round(float(b.get("amount")), 2)
            ty = int(b.get("tax_year") or tax_year(d.isoformat()[:7]))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "need date (YYYY-MM-DD), amount, optional tax_year") from e
        if amount <= 0 or not (d.year - 1 <= ty <= d.year):
            raise HTTPException(400, "amount must be > 0 and tax year must be the date's year or the one before")
        cur = db.conn.execute("INSERT INTO roth_contributions(date, amount, tax_year, note, at) VALUES (?,?,?,?,?)",
                              (d.isoformat(), amount, ty, str(b.get("note") or "")[:200], now_iso()))
        db.conn.commit()
        return {"id": cur.lastrowid, "tax_year": ty}

    @app.post("/api/roth/{cid}/delete")
    def roth_delete(cid: int, _: str = Depends(user), __=Depends(same_origin)) -> dict:
        if db.conn.execute("DELETE FROM roth_contributions WHERE id=?", (cid,)).rowcount == 0:
            raise HTTPException(404, "no such contribution")
        db.conn.commit()
        return {"ok": True}

    @app.post("/api/months/{month}/skip-roth")
    async def skip_roth(month: str, request: Request, _: str = Depends(user), __=Depends(same_origin)) -> dict:
        import re as _re
        from .db import now_iso
        if not _re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month must be YYYY-MM")
        if db.conn.execute("SELECT 1 FROM month_closes WHERE month=?", (month,)).fetchone():
            raise HTTPException(409, f"{month} is closed; reopen it to change its Roth skip")
        if (await request.json()).get("skip"):
            db.conn.execute("INSERT OR IGNORE INTO roth_skips(month, at) VALUES (?,?)", (month, now_iso()))
        else:
            db.conn.execute("DELETE FROM roth_skips WHERE month=?", (month,))
        db.conn.commit()
        return {"ok": True}

    @app.post("/api/bonus/{transaction_id}")
    async def bonus_decide(transaction_id: str, request: Request, _: str = Depends(user),
                           __=Depends(same_origin)) -> dict:
        from .db import now_iso
        decision = (await request.json()).get("decision")
        if decision not in ("bonus", "paycheck", None):
            raise HTTPException(400, "decision must be bonus, paycheck or null")
        row = db.conn.execute("SELECT date FROM transactions WHERE transaction_id=?", (transaction_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "no such transaction")
        if db.conn.execute("SELECT 1 FROM month_closes WHERE month=?", (row[0][:7],)).fetchone():
            raise HTTPException(409, "that month is closed; reopen it first")
        if decision is None:
            db.conn.execute("DELETE FROM bonus_decisions WHERE transaction_id=?", (transaction_id,))
        else:
            db.conn.execute("INSERT INTO bonus_decisions(transaction_id, decision, at) VALUES (?,?,?) "
                            "ON CONFLICT(transaction_id) DO UPDATE SET decision=excluded.decision, at=excluded.at",
                            (transaction_id, decision, now_iso()))
        db.conn.commit()
        return {"ok": True}

    # --- v2 dashboard ---------------------------------------------------------
    @app.get("/api/overview")
    def overview(_: str = Depends(user)) -> dict:
        from .classify import WaterfallConfigError
        from .dashboard import overview as build
        try:
            return build(db.conn, _cfg(), report.build(db)["items"])
        except WaterfallConfigError as e:
            raise HTTPException(503, f"waterfall config: {e}") from e

    @app.get("/api/spending")
    def spending(start: str, end: str, buckets: str = "spending", account: str = "", category: str = "",
                 q: str = "", _: str = Depends(user)) -> dict:
        from .classify import BUCKETS, WaterfallConfigError
        from .dashboard import spending as build
        bs = tuple(b for b in buckets.split(",") if b)
        if not bs or set(bs) - set(BUCKETS):
            raise HTTPException(400, f"buckets must be a comma list of {list(BUCKETS)}")
        try:
            return build(db.conn, _cfg(), start, end, bs, account or None,
                         tuple(c for c in category.split(",") if c), q[:80])
        except WaterfallConfigError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/rewards")
    def rewards(start: str = "", end: str = "", _: str = Depends(user)) -> dict:
        from .classify import WaterfallConfigError
        from .dashboard import rewards as build
        try:
            return build(db.conn, _cfg(), start or None, end or None)
        except WaterfallConfigError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/report.md", response_class=PlainTextResponse)
    def report_md(_: str = Depends(user)) -> str:
        return report.to_markdown(report.build(db), settings.plaid_env)

    return app


def app_factory() -> FastAPI:  # for `uvicorn --factory`
    return create_app()

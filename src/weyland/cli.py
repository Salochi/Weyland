"""Command line entry points (run as the `weyland` service user)."""

from __future__ import annotations

import argparse
import json
import sys

from . import report
from .config import ConfigError, load_settings
from .crypto import TokenBox, create_key
from .db import DB
from .plaid_client import PlaidClient, PlaidError
from .sync import Syncer

# Sandbox fixtures created without the Link UI. First Platypus Bank is Plaid's
# non-OAuth test institution; special test users only work with it.
SANDBOX_FIXTURES = {
    "bank": {
        "institution_id": "ins_109508",
        "initial_products": ["transactions", "liabilities"],
        "options": {"override_username": "user_transactions_dynamic", "override_password": "pass_good"},
        "name": "First Platypus Bank (sandbox: transactions)",
    },
    "investment": {
        "institution_id": "ins_109508",
        "initial_products": ["investments"],
        "options": {},
        "name": "First Platypus Bank (sandbox: investments)",
    },
}


def _classify(settings, month: str | None) -> int:
    """Read-only: no Plaid calls, no writes. Amounts are never printed."""
    import re
    from .classify import WaterfallConfigError, classify, load_config, summarize, unassigned_accounts
    try:
        cfg = load_config()
        db = DB(settings.db_path)
        rows = classify(db.conn, cfg, month)
        loose = unassigned_accounts(db.conn, cfg)
    except WaterfallConfigError as e:
        print(f"waterfall config error: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"month": month or "all", "unassigned_accounts": loose, **summarize(rows)}, indent=2))
    from .match import needs_review
    review = [r for r in rows if needs_review(r)]
    for r in review:
        print(f"review: {r.transaction_id} {r.date} {r.role:9} {re.sub(r'[0-9]', '#', r.name)[:50]} :: {r.reason}{' [' + r.match_status + ']' if r.match_status else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="weyland")
    ap.add_argument("--env", choices=["sandbox", "production"], help="override WEYLAND_PLAID_ENV")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-key", help="create the token encryption key if missing")
    sub.add_parser("check", help="validate config and Plaid credentials (no Items created)")
    sub.add_parser("sync", help="sync every connected Item")
    rp = sub.add_parser("report", help="print the coverage report")
    rp.add_argument("--json", action="store_true")
    cp = sub.add_parser("classify", help="classify transactions; prints counts + review queue (no amounts)")
    cp.add_argument("--month", help="YYYY-MM (default: all history)")
    op = sub.add_parser("override", help="pin a transaction's class (review queue)")
    op.add_argument("transaction_id")
    op.add_argument("cls", metavar="class", help="e.g. other_income, spending; 'clear' removes the override")
    op.add_argument("--note", default="")
    op.add_argument("--category", default=None, help="spending category label, e.g. rent")
    sp = sub.add_parser("sandbox-item", help="create a sandbox Item without Link (sandbox only)")
    sp.add_argument("kind", choices=sorted(SANDBOX_FIXTURES))
    dp = sub.add_parser("demo", help="serve the UI on made-up data (no Plaid keys, no config needed)")
    dp.add_argument("--host", default="127.0.0.1")
    dp.add_argument("--port", type=int, default=None)
    dp.add_argument("--dir", default=None, help="where to write the demo DB (default: a new temp dir)")
    args = ap.parse_args(argv)

    if args.cmd == "demo":  # before load_settings: the demo needs no config at all
        from .demo import DEFAULT_PORT, serve
        return serve(args.host, args.port or DEFAULT_PORT, args.dir)

    try:
        settings = load_settings(args.env)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "init-key":
        made = create_key(settings.key_path)
        print(f"{'created' if made else 'exists'}: {settings.key_path}")
        return 0

    if args.cmd == "classify":
        return _classify(settings, args.month)
    if args.cmd == "override":
        from .classify import WaterfallConfigError, set_override
        try:
            set_override(DB(settings.db_path).conn, args.transaction_id,
                         None if args.cls == "clear" else args.cls, args.note, args.category)
        except WaterfallConfigError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"ok: {args.transaction_id} -> {args.cls}")
        return 0

    plaid = PlaidClient(settings)
    try:
        if args.cmd == "check":
            # /link/token/create validates client_id+secret without creating an Item.
            plaid.call("/link/token/create", client_name="Weyland", language="en", country_codes=["US"],
                       user={"client_user_id": "weyland-check"}, products=["transactions"])
            print(f"ok: credentials valid for {settings.plaid_env}; db={settings.db_path}")
            return 0

        db = DB(settings.db_path)
        box = TokenBox(settings.key_path)
        syncer = Syncer(db, plaid, box)

        if args.cmd == "sandbox-item":
            if settings.is_production:
                print("refusing: sandbox-item only runs against sandbox", file=sys.stderr)
                return 2
            fx = SANDBOX_FIXTURES[args.kind]
            pub = plaid.call("/sandbox/public_token/create", institution_id=fx["institution_id"],
                             initial_products=fx["initial_products"],
                             options={**fx["options"], "transactions": {"days_requested": settings.days_requested}})
            ex = plaid.call("/item/public_token/exchange", public_token=pub["public_token"])
            db.add_item(ex["item_id"], box.seal(ex["access_token"]), fx["institution_id"], fx["name"], args.kind)
            print(json.dumps({"item_id": ex["item_id"], "results": syncer.sync_item(ex["item_id"])}))
            return 0
        if args.cmd == "sync":
            print(json.dumps(syncer.sync_all(), indent=2))
            return 0
        if args.cmd == "report":
            rep = report.build(db)
            print(json.dumps(rep, indent=2) if args.json else report.to_markdown(rep, settings.plaid_env))
            return 0
    except PlaidError as e:
        print(f"plaid error: {e.error_type}/{e.error_code}: {e.error_message} (request_id={e.request_id})",
              file=sys.stderr)
        return 1
    finally:
        plaid.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

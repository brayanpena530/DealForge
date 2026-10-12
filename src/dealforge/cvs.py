"""Agent-facing JSON CLI for a local CVS account snapshot.

Run ``python -m dealforge.cvs wallet --snapshot <local-json-file>``.
Run ``python -m dealforge.cvs sync`` to refresh an existing signed-in Chrome tab.
Single-checkout conditional calculations and bounded credit optimization are available.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Sequence

from dealforge.providers.cvs.wallet import AccountSnapshot, parse_timestamp, summarize_wallet


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    wallet = commands.add_parser("wallet", help="validate and summarize a local account snapshot")
    wallet.add_argument("--snapshot", type=Path, help="local normalized JSON; keep personal files outside the repo")
    wallet.add_argument("--as-of", help="ISO timestamp with the shopping location's UTC offset")
    wallet.add_argument("--max-age-hours", type=int, default=24, help="maximum snapshot age (default: 24)")
    login = commands.add_parser("login", help="sign in to a dedicated Playwright browser and verify session reuse")
    login.add_argument("--account-ref", default="cvs-personal", help="opaque local account label")
    login.add_argument("--timeout-seconds", type=int, default=600)
    sync = commands.add_parser("sync", help="refresh account inventory from the configured browser")
    sync.add_argument("--browser", choices=("chrome_mcp", "playwright"), help="override DEALFORGE_CVS_BROWSER for this command")
    sync.add_argument("--account-ref", default="cvs-personal", help="opaque local label, never a card number")
    sync.add_argument("--page-id", type=int, help="CVS tab ID when several ExtraCare tabs are open")
    sync.add_argument("--detail-limit", type=int, default=0, help="More details visits (0â€“25, default: inventory only)")
    sync.add_argument("--detail-query", default="", help="optional brand/item text filter for detail visits")
    sync.add_argument("--timeout-seconds", type=int, default=90)
    stores = commands.add_parser("stores", help="find CVS stores in a ZIP without changing selection")
    search = commands.add_parser("search", help="find product candidates and observed online prices")
    search.add_argument("query")
    search.add_argument("--brand", default="")
    search.add_argument("--size", default="")
    search.add_argument("--limit", type=int, default=10)
    product = commands.add_parser("product", help="inspect an exact CVS product URL")
    product.add_argument("product_url")
    ad = commands.add_parser("ad", help="search current public weekly-ad candidates")
    ad.add_argument("query")
    ad.add_argument("--zip", dest="zip_code", default=None)
    ad.add_argument("--limit", type=int, default=10)
    evaluate = commands.add_parser("evaluate", help="calculate a conditional basket from local JSON")
    evaluate.add_argument("--input", type=Path, required=True, help="JSON keyword arguments; keep personal inputs outside the repo")
    plan = commands.add_parser("plan", help="refresh and plan a whole CVS shopping list from local JSON")
    plan.add_argument("--input", type=Path, required=True, help="shopping-list JSON; keep personal data outside the repo")
    plan.add_argument("--browser", choices=("chrome_mcp", "playwright"))
    for name, help_text in (("prepare", "prepare account-bound rule drafts"),
                            ("optimize", "choose conditional coupons/credits for one checkout"),
                            ("eligibility", "assess source-bound offer eligibility"),
                            ("reconcile", "compare a saved plan to normalized receipt evidence")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--input", type=Path, required=True, help="local JSON keyword arguments; keep personal data outside the repo")
    for command in (stores, search, product):
        command.add_argument("--browser", choices=("chrome_mcp", "playwright"))
        command.add_argument("--zip", dest="zip_code", default=None)
        command.add_argument("--timeout-seconds", type=int, default=90)
        if command is not stores:
            command.add_argument("--store-id")
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = asyncio.run(_plan(args))
        print(json.dumps(result, ensure_ascii=True))
        return 2 if result["status"] == "invalid_input" else 0
    if args.command == "login":
        result = asyncio.run(_login(args))
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] == "ready" else 2
    if args.command == "ad":
        from dealforge.providers.cvs.weekly_ad import fetch_weekly_ad
        result = asyncio.run(fetch_weekly_ad(args.query, zip_code=args.zip_code, limit=args.limit))
        print(json.dumps(result, ensure_ascii=True))
        return 2 if result["status"] == "invalid_input" else 0
    if args.command in {"evaluate", "prepare", "optimize", "reconcile", "eligibility"}:
        from dealforge.providers.cvs.checkout import evaluate_basket
        from dealforge.providers.cvs.purchase_rules import prepare_purchase
        from dealforge.providers.cvs.optimizer import optimize_purchase
        from dealforge.providers.cvs.reconciliation import reconcile_receipt
        from dealforge.providers.cvs.offer_assessment import assess_offers
        action = {"evaluate":evaluate_basket, "prepare":prepare_purchase,
                  "optimize":optimize_purchase, "reconcile":reconcile_receipt, "eligibility":assess_offers}[args.command]
        try:
            request = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Basket input must be an object")
            result = action(**request)
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError):
            print(json.dumps({"status": "invalid_input", "reason": "invalid_basket_file_or_rules", "totals": None}))
            return 2
        print(json.dumps(result, ensure_ascii=True))
        return 0
    if args.command == "sync":
        result = asyncio.run(_sync(args))
        print(json.dumps(result, ensure_ascii=True))
        return 2 if result["status"] == "invalid_input" else 0
    if args.command in ("stores", "search", "product"):
        result = asyncio.run(_catalog(args))
        print(json.dumps(result, ensure_ascii=True))
        return 2 if result["status"] == "invalid_input" else 0
    if args.snapshot is None:
        print(json.dumps({
            "status": "needs_input",
            "next_action": "Provide --snapshot with a normalized local CVS account JSON file.",
            "live_sync_available": False,
        }))
        return 0
    try:
        snapshot = AccountSnapshot.from_dict(json.loads(args.snapshot.read_text(encoding="utf-8")))
        as_of = parse_timestamp(args.as_of) if args.as_of else datetime.now().astimezone()
        result = summarize_wallet(snapshot, as_of=as_of, max_age=timedelta(hours=args.max_age_hours))
    except (OSError, UnicodeError, json.JSONDecodeError, OverflowError):
        print(json.dumps({"status": "invalid_input", "error": "Could not read a valid snapshot or time range."}))
        return 2
    except ValueError as exc:
        print(json.dumps({"status": "invalid_input", "error": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


async def _sync(args) -> dict:
    from dealforge.providers.cvs.account_sync import SyncProblem
    from dealforge.providers.cvs.browser_backend import configured_collector, connection_advice
    import os
    previous = os.environ.get("DEALFORGE_CVS_BROWSER")
    if args.browser:
        os.environ["DEALFORGE_CVS_BROWSER"] = args.browser
    try:
        async with configured_collector() as collector:
            return await collector.sync(account_ref=args.account_ref, page_id=args.page_id,
                                        detail_limit=args.detail_limit, detail_query=args.detail_query,
                                        timeout_seconds=args.timeout_seconds)
    except ImportError:
        return SyncProblem("needs_input", "cvs_dependency_missing",
                           'Install the CVS extra: python -m pip install -e ".[cvs]"').to_dict()
    except SyncProblem as problem:
        return problem.to_dict()
    except Exception:
        return SyncProblem("browser_unavailable", "browser_bridge_start_failed",
                           connection_advice()).to_dict()
    finally:
        if args.browser:
            if previous is None:
                os.environ.pop("DEALFORGE_CVS_BROWSER", None)
            else:
                os.environ["DEALFORGE_CVS_BROWSER"] = previous


async def _catalog(args) -> dict:
    from dealforge.providers.cvs.account_sync import SyncProblem
    from dealforge.providers.cvs.browser_backend import configured_collector, connection_advice
    import os
    previous = os.environ.get("DEALFORGE_CVS_BROWSER")
    if args.browser:
        os.environ["DEALFORGE_CVS_BROWSER"] = args.browser
    try:
        async with configured_collector() as collector:
            options = {"zip_code": args.zip_code, "timeout_seconds": args.timeout_seconds}
            if args.command == "stores":
                return await collector.find_stores(**options)
            options["store_id"] = args.store_id
            if args.command == "search":
                return await collector.search_products(query=args.query, brand=args.brand, size=args.size,
                    limit=args.limit, **options)
            return await collector.get_product(product_url=args.product_url, **options)
    except ImportError:
        return SyncProblem("needs_input", "cvs_dependency_missing", 'Install the CVS extra: pip install -e ".[cvs]"').to_dict()
    except SyncProblem as problem:
        return problem.to_dict()
    except Exception:
        return SyncProblem("browser_unavailable", "browser_bridge_start_failed", connection_advice()).to_dict()
    finally:
        if args.browser:
            if previous is None:
                os.environ.pop("DEALFORGE_CVS_BROWSER", None)
            else:
                os.environ["DEALFORGE_CVS_BROWSER"] = previous


async def _login(args):
    from dealforge.providers.cvs.account_sync import SyncProblem
    try:
        from dealforge.providers.cvs.playwright_browser import login_and_verify
        return await login_and_verify(account_ref=args.account_ref, timeout_seconds=args.timeout_seconds,
            announce=lambda text: print(text, file=sys.stderr, flush=True))
    except ImportError:
        return SyncProblem("needs_input", "playwright_dependency_missing", 'Install the CVS extra: pip install -e ".[cvs]"').to_dict()
    except SyncProblem as problem:
        return problem.to_dict()
    except ValueError:
        return SyncProblem("invalid_input", "invalid_browser_settings", "Use a dedicated profile outside the repo and a valid browser configuration.").to_dict()
    except Exception:
        return SyncProblem("browser_unavailable", "playwright_login_failed", "Check the dedicated DealForge CVS browser, then retry dealforge-cvs login.").to_dict()


async def _plan(args):
    from dealforge.providers.cvs.account_sync import SyncProblem
    from dealforge.providers.cvs.browser_backend import configured_collector, connection_advice
    from dealforge.providers.cvs.planning import plan_trip, validate_request
    from dealforge.shopper_settings import shopping_zip
    import os
    previous = os.environ.get("DEALFORGE_CVS_BROWSER")
    if args.browser:
        os.environ["DEALFORGE_CVS_BROWSER"] = args.browser
    try:
        request = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Trip request must be an object")
        validate_request(request.get("items"), shopping_zip("cvs", request.get("zip_code")),
            request.get("store_id"), request.get("channel", "in_store"),
            request.get("detail_limit", 10), request.get("timeout_seconds", 180))
        async with configured_collector() as collector:
            return await plan_trip(collector, **request)
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, KeyError):
        return {"status": "invalid_input", "reason": "invalid_trip_file_or_evidence", "recommended": None}
    except ImportError:
        return SyncProblem("needs_input", "cvs_dependency_missing", 'Install the CVS extra: pip install -e ".[cvs]"').to_dict()
    except SyncProblem as problem:
        return problem.to_dict()
    except Exception:
        return SyncProblem("browser_unavailable", "browser_bridge_start_failed", connection_advice()).to_dict()
    finally:
        if args.browser:
            if previous is None:
                os.environ.pop("DEALFORGE_CVS_BROWSER", None)
            else:
                os.environ["DEALFORGE_CVS_BROWSER"] = previous


if __name__ == "__main__":
    raise SystemExit(main())

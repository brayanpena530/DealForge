"""MCP protocol test: synthetic browser, no Chrome connection or live account."""

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

import pytest

mcp = pytest.importorskip("mcp", reason="install the cvs extra for MCP protocol tests")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_server_exposes_only_sync_and_returns_normalized_data(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    script = tmp_path / "synthetic_server.py"
    script.write_text(f'''
import sys
sys.path.insert(0, {str(repo / 'src')!r})
sys.path.insert(0, {str(repo / 'tests')!r})
from contextlib import asynccontextmanager
import dealforge.cvs_mcp as app
from dealforge.providers.cvs.account_sync import AccountCollector
from dealforge.providers.cvs.account_state import AccountState
from pathlib import Path
from test_cvs_account_sync import Browser
class Collector(AccountCollector):
    async def find_stores(self, **options):
        return {{"status":"ready", "stores":[{{"store_id":"10001","zip_code":"00000","selected":True}}]}}
    async def get_product(self, **options):
        from datetime import datetime
        from test_cvs_checkout import lines
        product = lines("3.00", quantity=1)[0]["product"]
        product["name"] = "Example soap"
        product["price"]["observed_at"] = datetime.now().astimezone().isoformat()
        return {{"status":"partial","store":{{"store_id":"10001"}},"products":[product]}}
@asynccontextmanager
async def fake_collector():
    yield Collector(Browser(), poll_seconds=0, state=AccountState(Path({str(tmp_path / 'state')!r})))
app.chrome_collector = fake_collector
app.main()
''', encoding="utf-8")

    async def run():
        params = StdioServerParameters(command=sys.executable, args=[str(script)],
            env={"DEALFORGE_PREFERENCES_FILE": os.environ["DEALFORGE_PREFERENCES_FILE"]})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert {t.name for t in catalog.tools} == {"sync_cvs_account", "plan_cvs_trip", "assess_cvs_offer_eligibility", "find_cvs_stores", "search_cvs_products", "get_cvs_product", "match_cvs_products", "search_cvs_weekly_ad", "resolve_cvs_prices", "evaluate_cvs_basket", "prepare_cvs_purchase", "optimize_cvs_purchase", "reconcile_cvs_receipt"}
                assert all(t.annotations.readOnlyHint for t in catalog.tools)
                inventory = next(t for t in catalog.tools if t.name == "sync_cvs_account")
                assert "ctx" not in inventory.inputSchema["properties"]
                assert "detail_query" in inventory.inputSchema["properties"]
                result = await session.call_tool("sync_cvs_account", {})
                assert not result.isError
                data = result.structuredContent
                assert data["status"] == "partial" and data["live_sync_available"]
                assert data["collection"]["collected_records"] == 2
                assert "SECRET" not in json.dumps(data)
                assert data["account_identity_verified"] is False
                invalid = await session.call_tool("sync_cvs_account", {"detail_limit": 99})
                assert invalid.structuredContent["status"] == "invalid_input"
                invalid_plan = await session.call_tool("plan_cvs_trip", {"items": []})
                assert not invalid_plan.isError and invalid_plan.structuredContent["status"] == "invalid_input"
                again = await session.call_tool("sync_cvs_account", {})
                assert again.structuredContent["snapshot"]["rewards"][0]["reward_id"] == data["snapshot"]["rewards"][0]["reward_id"]
                invalid_search = await session.call_tool("search_cvs_products", {"query": "", "limit": 99})
                assert invalid_search.structuredContent["status"] == "invalid_input"
                matching = await session.call_tool("match_cvs_products", {"products": [], "offer_rules": []})
                assert matching.structuredContent["matches"] == [] and not matching.isError
                from test_cvs_checkout import lines, rule, NOW
                trip = await session.call_tool("plan_cvs_trip", {"items": [{"line_id":"soap","quantity":1,
                    "product_url":lines()[0]["product"]["url"],"query":"soap"}], "channel":"pickup",
                    "detail_limit":0,"extra_care_ready":True})
                assert not trip.isError and trip.structuredContent["status"] == "partial"
                assert trip.structuredContent["recommended"]["totals"]["cash_due_before_tax"] == "3.00"
                assert trip.structuredContent["store_id"] == "10001"
                options = {"lines": lines(), "channel": "in_store", "store_id": "10001", "as_of": NOW}
                resolved = await session.call_tool("resolve_cvs_prices", options)
                assert resolved.structuredContent["status"] == "resolved"
                basket = await session.call_tool("evaluate_cvs_basket", {**options,
                    "extra_care_ready": True, "coupons": [rule()]})
                assert basket.structuredContent["totals"]["cash_due_before_tax"] == "8.98"
                invalid_basket = await session.call_tool("evaluate_cvs_basket", {**options, "reward_value": "2.00"})
                assert invalid_basket.structuredContent["status"] == "invalid_input"
                invalid_ad = await session.call_tool("search_cvs_weekly_ad", {"query": "", "limit": 99})
                assert invalid_ad.structuredContent["status"] == "invalid_input"
                from test_cvs_optimizer import account, certificate, confirmations
                sample = account([certificate("expiring","10.00","2026-10-11")])
                data_lines = lines("3.00")
                payload = {"lines":data_lines,"account_sync":sample,"account_ref":"test-account",
                    "store_id":"10001","as_of":NOW,"allow_fixture":True,
                    "confirmations":confirmations(sample,data_lines)}
                draft = await session.call_tool("prepare_cvs_purchase",payload)
                assert draft.structuredContent["status"] == "prepared"
                eligibility = await session.call_tool("assess_cvs_offer_eligibility", payload)
                assert eligibility.structuredContent["preparation"]["status"] == "prepared"
                optimized = await session.call_tool("optimize_cvs_purchase",{**payload,"extra_care_ready":True})
                plan = optimized.structuredContent["recommended"]
                assert plan["redemption_order"] == ["expiring"]
                assert plan["totals"]["cash_due_before_tax"] == "0.00"
                from test_cvs_reconciliation import receipt
                compared = await session.call_tool("reconcile_cvs_receipt",{"plan":plan,"receipt":receipt(plan)})
                assert compared.structuredContent["status"] == "matched"
                assert not compared.structuredContent["real_receipt_reconciled"]
    asyncio.run(run())

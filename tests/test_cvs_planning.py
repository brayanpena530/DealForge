"""Whole-list orchestration: no live browser, credentials or shopper records."""

import asyncio
from copy import deepcopy
from datetime import datetime

import pytest

from dealforge.providers.cvs import planning
from test_cvs_checkout import lines, NOW, URL
from test_cvs_optimizer import account, certificate, confirmations


STORE = {"store_id": "10001", "zip_code": "00000", "address": "Synthetic store", "selected": True}


class Service:
    def __init__(self):
        self.calls = []
        self.data = lines("3.00")
        self.sync_result = account([certificate()])
        self.sync_result["snapshot"]["source"] = "browser"
        self.product_result = {"status": "partial", "store": STORE, "products": [self.data[0]["product"]]}

    async def sync(self, **options):
        self.calls.append(("sync", options))
        return deepcopy(self.sync_result)

    async def find_stores(self, **options):
        self.calls.append(("stores", options))
        return {"status": "partial", "stores": [STORE]}

    async def get_product(self, **options):
        self.calls.append(("product", options))
        return deepcopy(self.product_result)

    async def search_products(self, **options):
        self.calls.append(("search", options))
        return deepcopy(self.product_result)


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    class Clock:
        @staticmethod
        def now():
            return datetime.fromisoformat(NOW)
    monkeypatch.setattr(planning, "datetime", Clock)


def request(service, **kwargs):
    item = {k: v for k, v in service.data[0].items() if k != "product"}
    item.update(product_url=URL, query="Example soap", brand="Example")
    return dict(items=[item], account_ref="test-account", extra_care_ready=True, **kwargs)


def run(service, **kwargs):
    return asyncio.run(planning.plan_trip(service, **request(service, **kwargs)))


def test_complete_list_refreshes_and_returns_real_optimizer_alternatives():
    service = Service()
    attested = confirmations(service.sync_result, service.data)
    result = run(service, confirmations=attested)
    assert result["status"] == "conditional_estimate"
    assert result["recommended"]["redemption_order"] == ["old"]
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "1.00"
    assert result["optimization"]["no_credit"]["totals"]["cash_due_before_tax"] == "6.00"
    assert result["optimization"]["assumptions"]["return_days"] == [14, 28]
    assert [c[0] for c in service.calls] == ["sync", "stores", "product", "sync"]
    assert service.calls[-1][1]["detail_queries"] == ["Example"]
    assert not result["execution_ready"] and not result["checkout_verified"]
    assert "snapshot" not in result["account"]


def test_queries_return_choices_instead_of_auto_selecting_first_product():
    service = Service()
    item = {"line_id": "paste", "quantity": 2, "query": "Example soap"}
    result = asyncio.run(planning.plan_trip(service, items=[item], account_ref="test-account"))
    assert result["items"][0]["candidates"]
    assert result["next_actions"][0]["reason"] == "choose_exact_sku_url"
    assert not result["lines"] and result["recommended"] is None


def test_one_missing_item_suppresses_entire_basket_recommendation():
    service = Service()
    payload = request(service)
    payload["items"].append({"line_id": "other", "quantity": 1, "query": "Unknown item"})
    result = asyncio.run(planning.plan_trip(service, **payload))
    assert len(result["lines"]) == 1 and len(result["items"]) == 2
    assert result["recommended"] is None and result["optimization"] is None


def test_online_price_never_fills_missing_shelf_price():
    service = Service()
    payload = request(service)
    payload["items"][0]["price_evidence"] = []
    result = asyncio.run(planning.plan_trip(service, **payload))
    assert result["price_resolution"]["status"] == "needs_input"
    assert result["recommended"] is None
    assert any(a["kind"] == "price" for a in result["next_actions"])


def test_pickup_can_use_fresh_observed_online_price():
    service = Service()
    result = run(service, channel="pickup", detail_limit=0)
    assert result["price_resolution"]["status"] == "resolved"
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "6.00"
    assert result["status"] == "partial"  # Certificate terms still need confirmation.


@pytest.mark.parametrize("identity", ["first_seen", "different_account"])
def test_unmatched_account_stops_before_catalog(identity):
    service = Service()
    service.sync_result["account_binding"]["status"] = identity
    result = run(service)
    assert result["reason"] == "account_continuity_not_matched"
    assert len(service.calls) == 1 and result["recommended"] is None


def test_store_pin_cannot_silently_switch_store():
    service = Service()
    result = run(service, store_id="123")
    assert result["next_actions"][0]["reason"] == "requested_store_not_selected"
    assert [c[0] for c in service.calls] == ["sync", "stores"]


@pytest.mark.parametrize("mutation", [
    lambda r: r["products"][0].update(selected_sku_verified=False),
    lambda r: r["products"][0].update(sku_id="789", url=URL.replace("456", "789")),
    lambda r: r.update(store={**STORE, "store_id": "123"}),
])
def test_unverified_variant_or_changed_store_cannot_become_selected_basket(mutation):
    service = Service()
    mutation(service.product_result)
    result = run(service)
    assert not result["lines"] and result["recommended"] is None


def test_second_sync_account_change_removes_recommendation():
    service = Service()
    original = service.sync
    async def sync(**options):
        result = await original(**options)
        if "detail_queries" in options:
            return {"status": "needs_input", "reason": "account_changed_for_label", "snapshot": None}
        return result
    service.sync = sync
    result = run(service)
    assert result["recommended"] is None
    assert any(a.get("reason") == "account_changed_for_label" for a in result["next_actions"])


def test_deadline_retains_completed_stages_and_no_partial_estimate():
    service = Service()
    async def timeout(**options):
        raise asyncio.TimeoutError
    service.get_product = timeout
    result = run(service)
    assert result["reason"] == "planning_time_budget_reached" and result["account"]
    assert result["recommended"] is None


@pytest.mark.parametrize("patch", [
    {"items": []}, {"channel": "bogus"}, {"detail_limit": 26}, {"timeout_seconds": 200},
    {"items": [{"line_id": "x", "quantity": True, "query": "soap"}]},
    {"items": [{"line_id": "x", "quantity": 1, "product_url": "https://evil.test/"}]},
])
def test_invalid_requests_never_touch_browser(patch):
    service = Service()
    payload = {**request(service), **patch}
    with pytest.raises(ValueError):
        asyncio.run(planning.plan_trip(service, **payload))
    assert not service.calls


def test_shopping_inputs_are_not_mutated():
    service = Service()
    payload = request(service)
    before = deepcopy(payload)
    asyncio.run(planning.plan_trip(service, **payload))
    assert payload == before


def test_cli_plan_uses_same_pipeline_and_restores_backend(tmp_path, monkeypatch, capsys):
    import json
    import os
    from contextlib import asynccontextmanager
    from dealforge.cvs import main
    from dealforge.providers.cvs import browser_backend

    service = Service()
    @asynccontextmanager
    async def collector():
        assert os.environ["DEALFORGE_CVS_BROWSER"] == "playwright"
        yield service

    monkeypatch.setenv("DEALFORGE_CVS_BROWSER", "chrome_mcp")
    monkeypatch.setattr(browser_backend, "configured_collector", collector)
    file = tmp_path / "trip.json"
    file.write_text(json.dumps(request(service, confirmations=confirmations(service.sync_result, service.data))))
    assert main(["plan", "--browser", "playwright", "--input", str(file)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["recommended"]["totals"]["cash_due_before_tax"] == "1.00"
    assert os.environ["DEALFORGE_CVS_BROWSER"] == "chrome_mcp"


def test_cli_invalid_trip_does_not_launch_collector(tmp_path, monkeypatch, capsys):
    import json
    from dealforge.cvs import main
    from dealforge.providers.cvs import browser_backend
    def unexpected():
        raise AssertionError("Browser must not launch")
    monkeypatch.setattr(browser_backend, "configured_collector", unexpected)
    file = tmp_path / "invalid.json"
    file.write_text('{"items": []}')
    assert main(["plan", "--input", str(file)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid_input"

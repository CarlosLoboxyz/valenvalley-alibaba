"""JSON RFQ creation, the absent implicit default RFQ, and active-job lookup."""

from __future__ import annotations

import httpx
import pytest_asyncio

from .conftest import auth, build_app, make_settings, wait_for_job


@pytest_asyncio.fixture
async def client(tmp_path, fake_scrape):
    """Production-like: no implicit default RFQ is configured."""
    app = build_app(make_settings(tmp_path, rfq_excel_file=""), scrape_fn=fake_scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c

ITEMS = [
    {"name": "Claw hammer 16oz steel handle", "qty": 50, "unit": "pieces", "brand": "Stanley"},
    {"name": "Claw hammer 20oz fiberglass", "qty": 20, "unit": "pieces"},
    {"name": "Adjustable wrench 10 inch", "qty": 30, "unit": "pieces", "min_price": 2, "max_price": 9},
    {"name": "=HYPERLINK(\"http://evil\")", "qty": 1, "unit": "pieces"},
]


async def test_items_roundtrip_into_group(client):
    r = await client.post("/api/v1/rfq/items", headers=auth(), json={"filename": "Mi lista", "items": ITEMS})
    assert r.status_code == 200, r.text
    up = r.json()
    assert up["row_count"] == 4
    assert up["filename"] == "Mi lista.xlsx"
    assert up["preview"][0]["name"] == "Claw hammer 16oz steel handle"
    assert up["preview"][0]["qty"] == 50 and up["preview"][0]["brand"] == "Stanley"
    # formulas are stored as plain text, never evaluated
    assert up["preview"][3]["name"].startswith("=HYPERLINK")

    files = (await client.get("/api/v1/rfq/files", headers=auth())).json()
    assert [f["rfq_file_id"] for f in files] == [up["upload_id"]]

    g = await client.post("/api/v1/rfq/group", headers=auth(), json={"rfq_file_id": up["upload_id"]})
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["total_items"] == 4 and body["family_count"] >= 1
    assert body["estimated_credits_premium"] == body["uncached_families"] * 30


async def test_items_are_owner_scoped(client):
    up = (await client.post("/api/v1/rfq/items", headers=auth("tok-alice"), json={"items": ITEMS})).json()
    r = await client.post("/api/v1/rfq/group", headers=auth("tok-bob"), json={"rfq_file_id": up["upload_id"]})
    assert r.status_code == 404
    assert (await client.get("/api/v1/rfq/files", headers=auth("tok-bob"))).json() == []


async def test_items_validation(client):
    url = "/api/v1/rfq/items"
    assert (await client.post(url, json={"items": ITEMS})).status_code == 401
    for bad in (
        {"items": []},
        {"items": [{"name": ""}]},
        {"items": [{"name": "x", "qty": 0}]},
        {"items": [{"name": "x", "min_price": -1}]},
        {"items": [{"name": "x" * 301}]},
        {},
    ):
        assert (await client.post(url, headers=auth(), json=bad)).status_code == 422, bad


async def test_items_only_name_is_required(client):
    r = await client.post("/api/v1/rfq/items", headers=auth(), json={"items": [{"name": "Solo item"}]})
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 1


async def test_rfq_file_id_is_required(client):
    for path in ("/api/v1/rfq/group", "/api/v1/recommendations/score"):
        r = await client.post(path, headers=auth(), json={"limit": 5})
        assert r.status_code == 422, path
    r = await client.post("/api/v1/jobs/scrape", headers=auth(), json={"confirm_credit_spend": True})
    assert r.status_code == 422


async def test_no_default_rfq_when_not_configured(client):
    c = client
    assert (await c.get("/api/v1/rfq/files", headers=auth())).json() == []
    assert (await c.get("/api/v1/health")).json()["default_rfq_exists"] is False
    r = await c.post("/api/v1/rfq/group", headers=auth(), json={"rfq_file_id": "default"})
    assert r.status_code == 404 and r.json()["code"] == "rfq_not_found"
    r = await c.post("/api/v1/recommendations/score", headers=auth(), json={"rfq_file_id": "default"})
    assert r.status_code == 404


async def test_active_job_lifecycle(client):
    assert (await client.get("/api/v1/jobs/active", headers=auth())).json() is None
    assert (await client.get("/api/v1/jobs/active")).status_code == 401

    up = (await client.post("/api/v1/rfq/items", headers=auth(), json={"items": ITEMS[:3]})).json()
    r = await client.post(
        "/api/v1/jobs/scrape", headers=auth(),
        json={"rfq_file_id": up["upload_id"], "confirm_credit_spend": True},
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    active = (await client.get("/api/v1/jobs/active", headers=auth())).json()
    done = await wait_for_job(client, job_id)
    if active is not None:  # the fake scraper may finish before we look
        assert active["job_id"] == job_id and active["status"] in ("queued", "running")
    assert done["status"] == "completed"
    assert (await client.get("/api/v1/jobs/active", headers=auth())).json() is None
    # never leaks to another user
    assert (await client.get("/api/v1/jobs/active", headers=auth("tok-bob"))).json() is None

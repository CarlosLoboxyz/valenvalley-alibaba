"""Shared fixtures. No test may reach ScrapingBee or a real PocketBase."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from backend.app.core.auth import PocketBaseAuth
from backend.app.core.config import ROOT_DIR, Settings
from backend.app.main import create_app

SECRET = "sk-test-SECRET-KEY-12345"
TEMPLATE = ROOT_DIR / "templates" / "products.xlsx"

USERS = {
    "tok-alice": {"id": "alice0000000001", "email": "alice@example.com", "verified": True},
    "tok-bob": {"id": "bob00000000001", "email": "bob@example.com", "verified": True},
    "tok-unverified": {"id": "carol000000001", "email": "carol@example.com", "verified": False},
    "tok-admin": {"id": "admin0000000001", "email": "admin@example.com", "verified": True},
}


def _pocketbase_handler(request: httpx.Request) -> httpx.Response:
    """Fake PocketBase: only auth-refresh is needed."""
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if request.url.path.endswith("/auth-refresh") and token in USERS:
        return httpx.Response(200, json={"token": token, "record": USERS[token]})
    return httpx.Response(401, json={"message": "invalid"})


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        scrapingbee_api_key=SECRET,
        output_dir=str(tmp_path / "output"),
        debug_dir=str(tmp_path / "debug_raw"),
        rfq_excel_file=str(tmp_path / "products.xlsx"),
        request_delay_seconds=0.0,
        pocketbase_mirror=False,
        admin_emails="admin@example.com",
        cors_origins="http://localhost:5173",
        _env_file=None,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    shutil.copy(TEMPLATE, tmp_path / "products.xlsx")
    return make_settings(tmp_path)


def build_app(settings: Settings, **job_kwargs):
    app = create_app(settings)
    app.state.auth = PocketBaseAuth(
        settings, client=httpx.AsyncClient(transport=httpx.MockTransport(_pocketbase_handler))
    )
    jobs = app.state.services.jobs
    jobs._sleep_fn = job_kwargs.get("sleep_fn", lambda s: None)
    jobs._scrape_fn = job_kwargs.get("scrape_fn")
    return app


@pytest.fixture
def scrape_calls():
    return []


def fake_listing(i: int) -> dict:
    return {
        "title": f"Product {i} CPU processor",
        "price": f"US${10 + i}-{20 + i}",
        "moq": 2,
        "moq_unit": "pieces",
        "supplier": f"Supplier {i}",
        "supplier_id": f"sup{i}",
        "years_active": 5,
        "certifications": ["CE"],
        "has_trade_assurance": True,
        "url": f"https://example.com/p{i}",
    }


@pytest.fixture
def fake_scrape(scrape_calls):
    """Mock for scrape_term_detailed. Records calls and returns Spb-cost 30 per page."""

    def _scrape(term, api_key, page=1, stealth=False, max_products=15, debug_dir=None, **_):
        scrape_calls.append({"term": term, "page": page, "stealth": stealth, "api_key": api_key})
        return SimpleNamespace(
            products=[fake_listing(i) for i in range(3)], spb_cost=30, repaired=False, debug_path=None
        )

    return _scrape


@pytest_asyncio.fixture
async def client(settings, fake_scrape):
    app = build_app(settings, scrape_fn=fake_scrape)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


def auth(token: str = "tok-alice") -> dict:
    return {"Authorization": f"Bearer {token}"}


async def wait_for_job(client, job_id: str, token: str = "tok-alice", timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/api/v1/jobs/{job_id}", headers=auth(token))
        body = r.json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        await _sleep(0.05)
    raise AssertionError("job did not finish")


async def _sleep(s: float) -> None:
    import anyio

    await anyio.sleep(s)


@pytest.fixture(autouse=True)
def _block_live_network(monkeypatch):
    """Hard stop: any real requests call (ScrapingBee) fails the test."""
    import requests

    def _boom(*a, **k):
        raise AssertionError("Live HTTP call attempted in tests (ScrapingBee must be mocked)")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests.Session, "request", _boom)

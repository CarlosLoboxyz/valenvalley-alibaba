import json
import threading
from types import SimpleNamespace

import httpx

from .conftest import SECRET, auth, build_app, fake_listing, make_settings, wait_for_job

BODY = {"rfq_file_id": "default", "limit": 10, "confirm_credit_spend": True}


async def test_requires_confirm_credit_spend(client, scrape_calls):
    for payload in ({"rfq_file_id": "default", "limit": 10}, {**BODY, "confirm_credit_spend": False}):
        r = await client.post("/api/v1/jobs/scrape", headers=auth(), json=payload)
        assert r.status_code == 400
        assert r.json()["code"] == "credit_confirmation_required"
    assert scrape_calls == []


async def test_requires_auth_and_verified_email(client, scrape_calls):
    assert (await client.post("/api/v1/jobs/scrape", json=BODY)).status_code == 401
    r = await client.post("/api/v1/jobs/scrape", headers=auth("tok-unverified"), json=BODY)
    assert r.status_code == 403 and r.json()["code"] == "email_not_verified"
    assert scrape_calls == []


async def test_missing_api_key_is_503(settings, fake_scrape, tmp_path):
    s = make_settings(tmp_path, scrapingbee_api_key=None)
    app = build_app(s, scrape_fn=fake_scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)
        assert r.status_code == 503 and r.json()["code"] == "scrapingbee_not_configured"


async def test_full_job_checkpoints_cache_and_sums_credits(client, scrape_calls):
    r = await client.post("/api/v1/jobs/scrape", headers=auth(), json={**BODY, "pages": 2})
    assert r.status_code == 202, r.text
    created = r.json()
    assert created["status"] == "queued"
    assert created["projected_budget"]["estimated_credits"] == 3 * 2 * 30

    job = await wait_for_job(client, created["job_id"])
    assert job["status"] == "completed", job
    assert job["credits_spent"] == 3 * 2 * 30  # sum of Spb-cost
    assert job["progress"]["total_families"] == 3
    assert job["progress"]["completed_families"] == 3
    assert job["progress"]["live_fetches"] == 3
    assert len(scrape_calls) == 6
    assert all(c["api_key"] == SECRET for c in scrape_calls)  # key goes to the scraper only

    cache = (await client.get("/api/v1/cache", headers=auth())).json()
    assert cache["total"] == 3
    # Secret must not appear in any job/status payload
    assert SECRET not in json.dumps(job)

    # A second run finds everything cached: nothing to spend
    r2 = await client.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)
    assert r2.status_code == 400 and r2.json()["code"] == "nothing_to_scrape"
    # ...unless force_refresh is explicit
    r3 = await client.post("/api/v1/jobs/scrape", headers=auth(), json={**BODY, "force_refresh": True})
    assert r3.status_code == 202
    await wait_for_job(client, r3.json()["job_id"])


async def test_job_not_visible_to_other_user(client):
    jid = (await client.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
    await wait_for_job(client, jid)
    assert (await client.get(f"/api/v1/jobs/{jid}", headers=auth("tok-bob"))).status_code == 404
    assert (await client.post(f"/api/v1/jobs/{jid}/cancel", headers=auth("tok-bob"))).status_code == 404
    assert (await client.get("/api/v1/jobs/not-an-id", headers=auth())).status_code == 404


async def test_family_queries_subset_and_unknown(client, scrape_calls):
    plan = (await client.post("/api/v1/rfq/group", headers=auth(), json={"rfq_file_id": "default", "limit": 10})).json()
    one = plan["families"][0]["search_query"]
    r = await client.post("/api/v1/jobs/scrape", headers=auth(), json={**BODY, "family_queries": [one]})
    assert r.status_code == 202
    job = await wait_for_job(client, r.json()["job_id"])
    assert {c["term"] for c in scrape_calls} == {one}
    assert job["credits_spent"] == 30

    r = await client.post("/api/v1/jobs/scrape", headers=auth(), json={**BODY, "family_queries": ["bogus"]})
    assert r.status_code == 400 and r.json()["code"] == "unknown_family_query"


async def test_conflicting_job_returns_409(settings, tmp_path):
    gate = threading.Event()

    def slow_scrape(term, api_key, page=1, **_):
        gate.wait(10)
        return SimpleNamespace(products=[fake_listing(1)], spb_cost=30, repaired=False, debug_path=None)

    app = build_app(settings, scrape_fn=slow_scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        first = await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)
        assert first.status_code == 202
        # same user again
        r = await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)
        assert r.status_code == 409 and r.json()["code"] == "job_conflict"
        # a different user racing on the same queries
        r = await c.post("/api/v1/jobs/scrape", headers=auth("tok-bob"), json=BODY)
        assert r.status_code == 409
        gate.set()
        await wait_for_job(c, first.json()["job_id"])
        # guard released after completion
        r = await c.post("/api/v1/jobs/scrape", headers=auth(), json={**BODY, "force_refresh": True})
        assert r.status_code == 202
        await wait_for_job(c, r.json()["job_id"])


async def test_cancel_between_families_keeps_saved_work(settings):
    started = threading.Event()
    release = threading.Event()
    calls = []

    def scrape(term, api_key, page=1, **_):
        calls.append(term)
        started.set()
        release.wait(10)
        return SimpleNamespace(products=[fake_listing(1)], spb_cost=30, repaired=False, debug_path=None)

    app = build_app(settings, scrape_fn=scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
        assert started.wait(10)
        r = await c.post(f"/api/v1/jobs/{jid}/cancel", headers=auth())
        assert r.status_code == 200
        release.set()  # in-flight request finishes, then the flag is honoured
        job = await wait_for_job(c, jid)
        assert job["status"] == "cancelled"
        assert len(calls) == 1  # no further families were fetched
        assert job["credits_spent"] == 30
        cache = (await c.get("/api/v1/cache", headers=auth())).json()
        assert cache["total"] == 1  # the finished family stayed checkpointed
        # cancelling again: already terminal
        assert (await c.post(f"/api/v1/jobs/{jid}/cancel", headers=auth())).status_code == 409


async def test_failed_families_are_not_cached(settings):
    def failing(term, api_key, page=1, **_):
        raise RuntimeError(f"boom {api_key}")  # exception text contains the secret

    app = build_app(settings, scrape_fn=failing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
        job = await wait_for_job(c, jid)
        assert job["status"] == "failed"
        assert job["credits_spent"] == 0
        assert (await c.get("/api/v1/cache", headers=auth())).json()["total"] == 0  # retried next time
        assert SECRET not in json.dumps(job)  # redacted from warnings/errors


async def test_empty_result_not_cached(settings):
    def empty(term, api_key, page=1, **_):
        return SimpleNamespace(products=[], spb_cost=30, repaired=False, debug_path=None)

    app = build_app(settings, scrape_fn=empty)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
        job = await wait_for_job(c, jid)
        assert job["credits_spent"] == 90  # credits were still consumed and reported
        assert (await c.get("/api/v1/cache", headers=auth())).json()["total"] == 0


async def test_repaired_json_is_a_warning_not_a_failure(settings):
    def repaired(term, api_key, page=1, **_):
        return SimpleNamespace(products=[fake_listing(1)], spb_cost=30, repaired=True,
                               debug_path="/x/debug_raw/q_p1.txt")

    app = build_app(settings, scrape_fn=repaired)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        jid = (await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
        job = await wait_for_job(c, jid)
        assert job["status"] == "completed"
        assert any("repaired" in w for w in job["warnings"])
        assert any("q_p1.txt" in w for w in job["warnings"])
        assert "/x/debug_raw" not in json.dumps(job)  # only the file name, no server path


async def test_credit_caps(tmp_path, settings, fake_scrape, scrape_calls):
    app = build_app(make_settings(tmp_path, max_credits_per_job=50), scrape_fn=fake_scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)  # est 90 > 50
        assert r.status_code == 400 and r.json()["code"] == "credit_cap_exceeded"
    assert scrape_calls == []


async def test_sse_stream_emits_events_and_terminal(client):
    jid = (await client.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
    seen = []
    async with client.stream("GET", f"/api/v1/jobs/{jid}/events", headers=auth()) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                seen.append(line.split(":", 1)[1].strip())
    assert "family_completed" in seen and "credit_update" in seen and "progress" in seen
    assert seen[-1] == "completed"


async def test_sse_requires_auth(client):
    jid = (await client.post("/api/v1/jobs/scrape", headers=auth(), json=BODY)).json()["job_id"]
    assert (await client.get(f"/api/v1/jobs/{jid}/events")).status_code == 401
    assert (await client.get(f"/api/v1/jobs/{jid}/events", headers=auth("tok-bob"))).status_code == 404
    await wait_for_job(client, jid)


async def test_orphaned_jobs_marked_failed_on_startup(settings):
    app = build_app(settings)
    jobs = app.state.services.jobs
    jid = "a" * 32
    path = jobs.paths.job_path(jid)
    path.write_text(json.dumps({"job_id": jid, "owner_id": "alice0000000001", "status": "running"}))
    assert jobs.recover_orphans() == 1
    assert json.loads(path.read_text())["status"] == "failed"

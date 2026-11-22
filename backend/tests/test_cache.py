import json
import threading

import pytest

from backend.app.services.pipeline_bridge import pipeline

from .conftest import auth, fake_listing


def put(client, key="widget wholesale oem", n=3):
    client.app.state.services.cache.store.put(
        key, {"family_id": "F", "family_name": "w", "search_query": key.upper(),
              "products": [fake_listing(i) for i in range(n)], "scraped_at": "2026-01-01T00:00:00Z"})


async def test_list_and_get_entry(client):
    put(client)
    body = (await client.get("/api/v1/cache", headers=auth())).json()
    assert body["total"] == 1
    e = body["entries"][0]
    assert e["query_key"] == "widget wholesale oem" and e["listing_count"] == 3
    assert e["sample_suppliers"] == ["Supplier 0", "Supplier 1", "Supplier 2"]
    assert e["scraped_at"] == "2026-01-01T00:00:00Z"
    d = await client.get("/api/v1/cache/Widget Wholesale OEM", headers=auth())
    assert d.status_code == 200 and len(d.json()["products"]) == 3
    assert (await client.get("/api/v1/cache/missing", headers=auth())).status_code == 404


async def test_delete_is_admin_only_and_removes_single_entry(client):
    put(client, "a query")
    put(client, "b query")
    assert (await client.delete("/api/v1/cache/a query", headers=auth("tok-alice"))).status_code == 403
    assert (await client.delete("/api/v1/cache/a query")).status_code == 401
    r = await client.delete("/api/v1/cache/a query", headers=auth("tok-admin"))
    assert r.status_code == 204
    keys = [e["query_key"] for e in (await client.get("/api/v1/cache", headers=auth())).json()["entries"]]
    assert keys == ["b query"]
    assert (await client.delete("/api/v1/cache/a query", headers=auth("tok-admin"))).status_code == 404


async def test_debug_files_admin_only_and_lists_names_only(client, settings):
    from pathlib import Path

    d = Path(settings.debug_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "q_p1.txt").write_text("raw")
    assert (await client.get("/api/v1/debug/files", headers=auth())).status_code == 403
    r = await client.get("/api/v1/debug/files", headers=auth("tok-admin"))
    assert r.json() == {"files": ["q_p1.txt"]}


async def test_corrupt_cache_does_not_break_health(client, settings):
    from pathlib import Path

    (Path(settings.output_dir) / "family_scrape_cache.json").write_text("{not json")
    assert (await client.get("/api/v1/health")).json()["cache_entries"] == 0
    assert (await client.get("/api/v1/cache", headers=auth())).status_code == 500


def test_atomic_write_leaves_no_partial_file_and_survives_concurrency(tmp_path):
    store = pipeline.FileCacheStore(tmp_path / "cache.json")

    def writer(n):
        for i in range(25):
            store.put(f"k{n}-{i}", {"products": [{"title": "x"}] * 20})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    data = json.loads((tmp_path / "cache.json").read_text())  # valid JSON, no lost updates
    assert len(data) == 150
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_keeps_previous_cache_intact(tmp_path, monkeypatch):
    store = pipeline.FileCacheStore(tmp_path / "cache.json")
    store.put("a", {"products": []})
    import os

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.put("b", {"products": []})
    monkeypatch.undo()
    assert list(json.loads((tmp_path / "cache.json").read_text())) == ["a"]


def test_normalize_listing_is_idempotent():
    clean = fake_listing(1)
    assert pipeline.normalize_listing(clean) is clean
    raw = {"title": "t", "moq_raw": "20 pieces", "supplier_url": "https://x.en.alibaba.com/a",
           "years_active": "3 yrs", "certifications": "CE", "has_trade_assurance": "true"}
    once = pipeline.normalize_listing(raw)
    assert once["moq"] == 20 and once["years_active"] == 3 and once["has_trade_assurance"] is True
    assert pipeline.normalize_listing(once) == once

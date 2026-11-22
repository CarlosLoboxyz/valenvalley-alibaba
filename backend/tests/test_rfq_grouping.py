import io

from openpyxl import Workbook

from .conftest import TEMPLATE, auth


def _xlsx(rows, headers=("position", "name", "qty")) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _upload(client, data: bytes, name="rfq.xlsx", token="tok-alice"):
    return await client.post(
        "/api/v1/rfq/upload", headers=auth(token), files={"file": (name, data, "application/octet-stream")}
    )


async def test_upload_template_and_list(client):
    r = await _upload(client, TEMPLATE.read_bytes(), "../../evil name.xlsx")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 100
    assert body["filename"] == "evil name.xlsx"  # path components stripped
    assert len(body["preview"]) == 10
    files = (await client.get("/api/v1/rfq/files", headers=auth())).json()
    ids = {f["rfq_file_id"] for f in files}
    assert {"default", body["upload_id"]} <= ids


async def test_upload_rejects_non_xlsx_and_garbage(client):
    assert (await _upload(client, b"x", "a.csv")).status_code == 400
    r = await _upload(client, b"not a zip at all", "a.xlsx")
    assert r.status_code == 400 and r.json()["code"] == "invalid_rfq"
    r = await _upload(client, _xlsx([[1, "x", 1]], headers=("foo", "bar", "baz")))
    assert r.status_code == 400 and r.json()["code"] == "invalid_rfq"


async def test_upload_size_limit(settings, fake_scrape):
    import httpx

    from .conftest import build_app, make_settings

    s = make_settings(settings.rfq_excel_file and __import__("pathlib").Path(settings.rfq_excel_file).parent, max_upload_mb=1)
    app = build_app(s, scrape_fn=fake_scrape)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/v1/rfq/upload", headers=auth(),
            files={"file": ("big.xlsx", b"PK\x03\x04" + b"0" * (2 * 1024 * 1024), "application/octet-stream")},
        )
        assert r.status_code == 413


async def test_upload_isolated_between_users(client):
    up = (await _upload(client, TEMPLATE.read_bytes())).json()["upload_id"]
    body = {"rfq_file_id": up, "limit": 5}
    assert (await client.post("/api/v1/rfq/group", headers=auth("tok-alice"), json=body)).status_code == 200
    r = await client.post("/api/v1/rfq/group", headers=auth("tok-bob"), json=body)
    assert r.status_code == 404  # not 403: ids cannot be probed
    bobs = (await client.get("/api/v1/rfq/files", headers=auth("tok-bob"))).json()
    assert up not in {f["rfq_file_id"] for f in bobs}


async def test_rfq_id_path_traversal(client):
    for bad in ("../../etc/passwd", "..%2f..%2fx", "a" * 40):
        r = await client.post("/api/v1/rfq/group", headers=auth(), json={"rfq_file_id": bad})
        assert r.status_code in (404, 422), bad


async def test_group_is_zero_credit_and_reports_estimates(client, scrape_calls):
    r = await client.post("/api/v1/rfq/group", headers=auth(), json={"limit": 10, "pages": 2})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["total_items"] == 10 and b["family_count"] == 3
    assert b["cached_families"] == 0 and b["uncached_families"] == 3
    assert b["estimated_credits_premium"] == 3 * 2 * 30
    assert b["estimated_credits_stealth"] == 3 * 2 * 80
    assert b["estimated_credits_selected"] == b["estimated_credits_premium"]
    assert {"family_id", "family_name", "search_query", "cohesion_score", "positions", "is_cached",
            "cached_listing_count"} <= set(b["families"][0])
    assert scrape_calls == []  # grouping never scrapes


async def test_group_stealth_selected(client):
    b = (await client.post("/api/v1/rfq/group", headers=auth(), json={"limit": 10, "stealth": True})).json()
    assert b["estimated_credits_selected"] == b["estimated_credits_stealth"] == 3 * 80


async def test_group_invalid_granularity_and_positions(client):
    r = await client.post("/api/v1/rfq/group", headers=auth(), json={"granularity": "huge"})
    assert r.status_code == 422
    r = await client.post("/api/v1/rfq/group", headers=auth(), json={"positions": "1-,"})
    assert r.status_code == 400


async def test_group_positions_filter(client):
    b = (await client.post("/api/v1/rfq/group", headers=auth(), json={"positions": "1-4"})).json()
    assert b["total_items"] == 4

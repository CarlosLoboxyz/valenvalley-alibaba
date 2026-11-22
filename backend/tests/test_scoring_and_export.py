import io

from openpyxl import load_workbook

from .conftest import auth, fake_listing


def seed_cache(client, queries: list[str], raw_shape: bool = False):
    store = client.app.state.services.cache.store
    for q in queries:
        if raw_shape:  # shape written by the pre-fix scraper
            products = [
                {"title": "AMD Ryzen CPU core", "price": "US$250-300", "moq_raw": "2 pieces",
                 "supplier": "RawCo", "supplier_url": "https://rawco.en.alibaba.com/x",
                 "years_active": "6 yrs", "certifications": "CE, RoHS", "has_trade_assurance": True,
                 "url": "//a.com/p?x=1"},
            ]
        else:
            products = [fake_listing(i) for i in range(4)]
        store.put(q.strip().lower(), {"family_id": "X", "family_name": "x", "search_query": q, "products": products})


async def families(client, limit=10):
    return (await client.post("/api/v1/rfq/group", headers=auth(), json={"limit": limit})).json()["families"]


async def test_score_zero_credit_four_tabs_and_dashboard(client, scrape_calls):
    fams = await families(client)
    seed_cache(client, [f["search_query"] for f in fams[:2]])  # third family intentionally missing
    r = await client.post("/api/v1/recommendations/score", headers=auth(), json={"limit": 10, "top_n": 3})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["credits_consumed"] == 0 and scrape_calls == []
    assert len(b["recommendations"]) == 10
    assert all(len(p["vendors"]) <= 3 for p in b["recommendations"])
    assert len(b["family_search_plan"]) == 3
    assert isinstance(b["cross_item_vendors"], list) and isinstance(b["disqualified"], list)
    d = b["dashboard"]
    assert sum(d["tier_distribution"].values()) > 0
    assert d["missing_cache_families"] == [fams[2]["family_id"]]
    assert any(fams[2]["family_id"] in w for w in b["warnings"])
    assert d["positions_without_vendors"] >= 1
    assert abs(sum(b["effective_weights"]["vendor"].values()) - 100) < 0.1
    assert b["effective_weights"]["blend"] == {"vendor_score": 75.0, "rfq_fit": 25.0}


async def test_score_does_not_need_api_key(settings, fake_scrape):
    import httpx

    from .conftest import build_app, make_settings

    app = build_app(make_settings(__import__("pathlib").Path(settings.rfq_excel_file).parent, scrapingbee_api_key=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/recommendations/score", headers=auth(), json={"limit": 5})
        assert r.status_code == 200


async def test_legacy_raw_cache_is_normalized_on_read(client):
    fams = await families(client)
    seed_cache(client, [f["search_query"] for f in fams], raw_shape=True)
    b = (await client.post("/api/v1/recommendations/score", headers=auth(),
                           json={"limit": 10, "persist_artifacts": False})).json()
    vendors = [v for p in b["recommendations"] for v in p["vendors"]]
    assert vendors, "raw-shaped cache must still produce vendors"
    v = vendors[0]
    assert v["best_offer"]["moq"] == 2 and v["years_active"] == 6 and v["supplier_id"]
    detail = (await client.get(f"/api/v1/cache/{fams[0]['search_query']}", headers=auth())).json()
    assert detail["products"][0]["moq"] == 2 and detail["products"][0]["certifications"] == ["CE", "RoHS"]


async def test_invalid_weights_rejected(client):
    for payload in ({"weight_price": -1}, {"weight_vendor_score": 0, "weight_rfq_fit": 0},
                    {"weight_price": 0, "weight_moq": 0, "weight_trade_assurance": 0,
                     "weight_years": 0, "weight_certs": 0}):
        r = await client.post("/api/v1/recommendations/score", headers=auth(), json={"limit": 3, **payload})
        assert r.status_code == 400 and r.json()["code"] == "invalid_weights", payload


async def test_weights_are_normalized_and_reported(client):
    r = await client.post("/api/v1/recommendations/score", headers=auth(),
                          json={"limit": 3, "weight_price": 100, "weight_moq": 0, "weight_trade_assurance": 0,
                                "weight_years": 0, "weight_certs": 0, "weight_vendor_score": 50,
                                "weight_rfq_fit": 50, "persist_artifacts": False})
    w = r.json()["effective_weights"]
    assert w["vendor"]["price"] == 100.0 and w["blend"] == {"vendor_score": 50.0, "rfq_fit": 50.0}


async def test_hard_filters_move_vendors_to_disqualified(client):
    fams = await families(client)
    seed_cache(client, [f["search_query"] for f in fams])
    ok = (await client.post("/api/v1/recommendations/score", headers=auth(),
                            json={"limit": 10, "persist_artifacts": False})).json()
    strict = (await client.post("/api/v1/recommendations/score", headers=auth(),
                                json={"limit": 10, "min_years": 50, "persist_artifacts": False})).json()
    assert len(strict["disqualified"]) > len(ok["disqualified"])
    assert all(len(p["vendors"]) == 0 for p in strict["recommendations"])
    assert any("year" in " ".join(d["reasons"]).lower() for d in strict["disqualified"])


async def test_latest_empty_then_populated_and_user_scoped(client):
    r = await client.get("/api/v1/recommendations/latest", headers=auth())
    assert r.status_code == 200 and r.json() == {"exists": False, "result": None}
    fams = await families(client)
    seed_cache(client, [f["search_query"] for f in fams])
    await client.post("/api/v1/recommendations/score", headers=auth(), json={"limit": 10})
    latest = (await client.get("/api/v1/recommendations/latest", headers=auth())).json()
    assert latest["exists"] and len(latest["result"]["recommendations"]) == 10
    bob = (await client.get("/api/v1/recommendations/latest", headers=auth("tok-bob"))).json()
    assert bob["exists"] is False


async def test_export_xlsx_has_four_tabs_matching_json(client):
    assert (await client.get("/api/v1/recommendations/export/xlsx", headers=auth())).status_code == 404
    fams = await families(client)
    seed_cache(client, [f["search_query"] for f in fams])
    body = (await client.post("/api/v1/recommendations/score", headers=auth(),
                              json={"limit": 10, "top_n": 2})).json()
    r = await client.get("/api/v1/recommendations/export/xlsx", headers=auth())
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert "attachment" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Recommendations", "Family_Search_Plan", "Cross_Item_Vendors", "Disqualified"]
    rec_rows = wb["Recommendations"].max_row - 1
    json_rows = sum(max(len(p["vendors"]), 1) for p in body["recommendations"])
    assert rec_rows == json_rows
    assert wb["Family_Search_Plan"].max_row - 1 == len(body["family_search_plan"])
    assert wb["Cross_Item_Vendors"].max_row - 1 == len(body["cross_item_vendors"])
    assert wb["Disqualified"].max_row - 1 == len(body["disqualified"])
    # another user cannot download it
    assert (await client.get("/api/v1/recommendations/export/xlsx", headers=auth("tok-bob"))).status_code == 404


async def test_persist_false_writes_nothing(client):
    await client.post("/api/v1/recommendations/score", headers=auth(), json={"limit": 3, "persist_artifacts": False})
    assert (await client.get("/api/v1/recommendations/latest", headers=auth())).json()["exists"] is False

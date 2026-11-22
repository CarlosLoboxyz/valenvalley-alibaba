from .conftest import SECRET, auth


async def test_health_is_public(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["scrapingbee_configured"] is True
    assert body["default_rfq_exists"] is True
    assert body["cache_entries"] == 0


async def test_config_never_leaks_secret(client):
    r = await client.get("/api/v1/config")
    assert r.status_code == 200
    text = r.text.lower()
    assert SECRET.lower() not in text
    assert "api_key" not in text and "scrapingbee_api_key" not in text
    body = r.json()
    assert body["credit_rates"] == {"premium_per_page": 30, "stealth_per_page": 80}
    assert body["granularities"] == ["fine", "balanced", "broad"]


async def test_protected_routes_require_token(client):
    for method, path in [
        ("get", "/api/v1/rfq/files"),
        ("get", "/api/v1/cache"),
        ("get", "/api/v1/recommendations/latest"),
        ("get", "/api/v1/me"),
    ]:
        r = await getattr(client, method)(path)
        assert r.status_code == 401, path
        assert r.json()["code"] == "missing_token"


async def test_invalid_token_rejected(client):
    r = await client.get("/api/v1/me", headers=auth("nope"))
    assert r.status_code == 401
    assert r.json()["code"] == "invalid_token"


async def test_me(client):
    r = await client.get("/api/v1/me", headers=auth())
    assert r.json() == {"id": "alice0000000001", "email": "alice@example.com", "verified": True}


async def test_error_payload_shape(client):
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404
    assert set(r.json()) == {"detail", "code"}


async def test_validation_error_is_422_without_input_echo(client):
    r = await client.post("/api/v1/rfq/group", headers=auth(), json={"pages": 0, "positions": "abc"})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error"
    assert "abc" not in r.text

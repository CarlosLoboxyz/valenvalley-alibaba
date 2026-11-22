"""PocketBase-backed authentication.

The SvelteKit frontend server signs users in against PocketBase and forwards the
user's token as ``Authorization: Bearer <token>``. We validate it by asking
PocketBase to refresh it (``auth-refresh``) and cache the verdict briefly, keyed by a
hash of the token so raw tokens are never kept in memory longer than a request.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import Depends, Request

from .config import Settings
from .errors import ApiError
from .paths import USER_ID_RE


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    verified: bool
    token: str = ""  # raw bearer token; used only to mirror records to PocketBase as the user


class PocketBaseAuth:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._cache: dict[str, tuple[float, AuthUser]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def authenticate(self, token: str) -> AuthUser:
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.monotonic()
        hit = self._cache.get(digest)
        if hit and hit[0] > now:
            return hit[1]

        url = (
            f"{self.settings.pocketbase_url.rstrip('/')}/api/collections/"
            f"{self.settings.pocketbase_users_collection}/auth-refresh"
        )
        try:
            resp = await self._client.post(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise ApiError(503, "auth_unavailable", "Authentication service unavailable") from exc

        if resp.status_code in (400, 401, 403, 404):
            raise ApiError(401, "invalid_token", "Invalid or expired token")
        if resp.status_code != 200:
            raise ApiError(503, "auth_unavailable", "Authentication service unavailable")

        record = resp.json().get("record") or {}
        user_id = str(record.get("id", ""))
        if not USER_ID_RE.match(user_id):
            raise ApiError(401, "invalid_token", "Invalid or expired token")
        user = AuthUser(
            id=user_id,
            email=str(record.get("email", "")),
            verified=bool(record.get("verified", False)),
            token=token,
        )
        self._cache[digest] = (now + self.settings.auth_cache_ttl_seconds, user)
        if len(self._cache) > 2048:  # bound memory
            for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                self._cache.pop(k, None)
        return user


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "missing_token", "Authentication required")
    return token.strip()


async def get_current_user(request: Request) -> AuthUser:
    auth: PocketBaseAuth = request.app.state.auth
    return await auth.authenticate(_bearer(request))


async def require_verified_user(
    request: Request, user: AuthUser = Depends(get_current_user)
) -> AuthUser:
    """Credit-spending routes require a verified email (unless disabled in settings)."""
    if request.app.state.settings.require_verified_email and not user.verified:
        raise ApiError(403, "email_not_verified", "Verify your email before spending credits")
    return user


async def require_admin(request: Request, user: AuthUser = Depends(get_current_user)) -> AuthUser:
    """Shared-state operations (cache eviction, raw debug payloads) are admin-only."""
    admins = request.app.state.settings.admin_email_set
    if not (user.verified and user.email.lower() in admins):
        raise ApiError(403, "admin_required", "Administrator access required")
    return user

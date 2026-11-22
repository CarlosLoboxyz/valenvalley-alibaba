"""Best-effort mirror of upload/job/run metadata into PocketBase.

Records are written *as the user* (their own bearer token), so PocketBase API rules
(``owner = @request.auth.id``) apply and the SvelteKit frontend can list history and
subscribe to job progress with the PocketBase SDK. Failures are logged and swallowed:
the filesystem copy under ``output/`` stays the source of truth.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ..core.auth import AuthUser
from ..core.config import Settings

log = logging.getLogger("app.pocketbase")


class PocketBaseMirror:
    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None):
        self.enabled = settings.pocketbase_mirror
        self.base = settings.pocketbase_url.rstrip("/")
        self._client = client or httpx.Client(timeout=5.0)

    def upsert(
        self, user: AuthUser, collection: str, key_field: str, key_value: str, data: dict[str, Any]
    ) -> None:
        if not self.enabled or not user.token:
            return
        headers = {"Authorization": f"Bearer {user.token}"}
        body = {**data, key_field: key_value, "owner": user.id}
        base = f"{self.base}/api/collections/{collection}/records"
        try:
            found = self._client.get(
                base,
                headers=headers,
                params={"filter": f'({key_field}="{key_value}")', "perPage": 1, "fields": "id"},
            )
            found.raise_for_status()
            items = found.json().get("items", [])
            if items:
                resp = self._client.patch(f"{base}/{items[0]['id']}", headers=headers, json=body)
            else:
                resp = self._client.post(base, headers=headers, json=body)
            resp.raise_for_status()
        except Exception as exc:  # never fail a request because the mirror is down
            log.warning("PocketBase mirror to %s failed: %s", collection, type(exc).__name__)

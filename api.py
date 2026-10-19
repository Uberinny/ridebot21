import asyncio
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import aiohttp


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None, request_id: str | None = None):
        super().__init__(f"[{status}] {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id


class PartnerClient:
    """Async client for the Partner Gateway API (HMAC-SHA256 signed requests)."""

    def __init__(self, base_url: str, key_id: str, secret: str, sign_order: list[str]):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.secret = secret.encode()
        self.sign_order = sign_order
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _signature_headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        parts = {
            "method": method.upper(),
            "path": path,
            "timestamp": timestamp,
            "nonce": nonce,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        canonical = "\n".join(parts[name] for name in self.sign_order)
        signature = hmac.new(self.secret, canonical.encode(), hashlib.sha256).hexdigest()
        return {
            "X-Api-Key": self.key_id,
            "X-Api-Timestamp": timestamp,
            "X-Api-Nonce": nonce,
            "X-Api-Signature": f"v1={signature}",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        await self.start()
        assert self._session is not None

        path_qs = path
        if params:
            path_qs = f"{path}?{urlencode({k: v for k, v in params.items() if v is not None})}"

        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        req_headers = {"Accept": "application/json"}
        if body is not None:
            req_headers["Content-Type"] = "application/json"
        if signed:
            req_headers.update(self._signature_headers(method, path_qs, raw))
        if headers:
            req_headers.update(headers)

        async with self._session.request(
            method, self.base_url + path_qs, data=raw or None, headers=req_headers
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                data = None

            if resp.status >= 400 or not isinstance(data, dict) or data.get("ok") is False:
                err = (data or {}).get("error") or {}
                code = err.get("code") or f"http_{resp.status}"
                message = err.get("message") or (await resp.text())[:300]
                if resp.status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        message = f"{message} (retry after {retry_after}s)"
                raise ApiError(resp.status, code, message, err.get("details"), (data or {}).get("request_id"))
            return data

    # Meta
    async def health(self) -> dict:
        return await self.request("GET", "/api/v1/health", signed=False)

    async def ping(self) -> dict:
        return await self.request("GET", "/api/v1/ping")

    # Slots
    async def list_slots(self, include_warming: bool = False, include_disabled: bool = False) -> list[dict]:
        params = {}
        if include_warming:
            params["ready"] = "0"
        if include_disabled:
            params["all"] = "1"
        data = await self.request("GET", "/api/v1/slots", params=params or None)
        return data.get("slots", [])

    async def get_slot(self, slot_id: str) -> dict:
        data = await self.request("GET", f"/api/v1/slots/{slot_id}")
        return data["slot"]

    # Members
    async def register_member(self, member_id: str, display_name: str | None = None) -> dict:
        body = {"member_id": member_id}
        if display_name:
            body["display_name"] = display_name
        data = await self.request("POST", "/api/v1/members/register", body=body)
        return data["member"]

    async def list_cards(self, member_id: str) -> list[dict]:
        data = await self.request("GET", "/api/v1/members/cards", params={"member_id": member_id})
        return data.get("cards", [])

    async def save_card(
        self, member_id: str, card: dict, label: str | None = None, make_default: bool | None = None
    ) -> dict:
        """Add a card to the member's pool. make_default=None keeps the API rule (first card becomes default)."""
        body: dict[str, Any] = {"member_id": member_id, "card": card}
        if make_default is not None:
            body["make_default"] = make_default
        if label:
            body["label"] = label
        data = await self.request("POST", "/api/v1/members/cards", body=body)
        return data["card"]

    async def set_default_card(self, member_id: str, card_id: str) -> dict:
        data = await self.request(
            "POST", f"/api/v1/members/cards/{card_id}/default", body={"member_id": member_id}
        )
        return data["card"]

    async def verify_card(self, member_id: str, card_id: str) -> dict:
        return await self.request("GET", f"/api/v1/members/cards/{card_id}", params={"member_id": member_id})

    async def delete_card(self, member_id: str, card_id: str) -> dict:
        return await self.request("DELETE", f"/api/v1/members/cards/{card_id}", params={"member_id": member_id})

    # Geo
    async def geo_search(self, query: str, bias: tuple[float, float] | None = None) -> dict:
        body: dict[str, Any] = {"query": query}
        if bias:
            body["bias"] = {"lat": bias[0], "lon": bias[1]}
        data = await self.request("POST", "/api/v1/geo/search", body=body)
        return data["result"]

    async def geo_resolve(self, place_id: str, title: str | None = None, subtitle: str | None = None) -> dict:
        body = {"place_id": place_id}
        if title:
            body["title"] = title
        if subtitle:
            body["subtitle"] = subtitle
        data = await self.request("POST", "/api/v1/geo/resolve", body=body)
        return data["result"]["location"]

    # Orders
    async def quote(self, member_id: str, slot_id: str, origin: Any, destination: Any, prefs: dict | None = None) -> dict:
        body: dict[str, Any] = {
            "member_id": member_id,
            "slot_id": slot_id,
            "origin": origin,
            "destination": destination,
        }
        if prefs:
            body["prefs"] = prefs
        data = await self.request("POST", "/api/v1/orders/quote", body=body)
        return data["result"]

    async def create_order(
        self,
        member_id: str,
        slot_id: str,
        origin: Any,
        destination: Any,
        tier: str = "standard",
        card_id: str | None = None,
        prefs: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "member_id": member_id,
            "slot_id": slot_id,
            "origin": origin,
            "destination": destination,
            "tier": tier,
        }
        if card_id:
            body["card_id"] = card_id
        if prefs:
            body["prefs"] = prefs
        data = await self.request(
            "POST",
            "/api/v1/orders/create",
            body=body,
            headers={"Idempotency-Key": idempotency_key or uuid.uuid4().hex},
        )
        return data["task"]

    async def get_task(self, task_id: str) -> dict:
        data = await self.request("GET", f"/api/v1/tasks/{task_id}")
        return data["task"]

    async def wait_for_task(self, task_id: str, timeout: float = 180, interval: float = 2.5) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            task = await self.get_task(task_id)
            if task.get("status") in ("succeeded", "failed"):
                return task
            if time.monotonic() >= deadline:
                return task
            await asyncio.sleep(interval)

    async def order_status(self, member_id: str, slot_id: str) -> dict:
        data = await self.request("POST", "/api/v1/orders/status", body={"member_id": member_id, "slot_id": slot_id})
        return data["result"]

    async def cancel_order(self, member_id: str, slot_id: str) -> dict:
        data = await self.request("POST", "/api/v1/orders/cancel", body={"member_id": member_id, "slot_id": slot_id})
        return data["result"]

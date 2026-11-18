"""
Provider API clients (duck-typed: add_order / get_status / get_balance).

  - EpinbyClient     -> modern REST JSON API (X-API-KEY header)
  - SmmGenericClient -> classic SMM-panel form API (action=add/status/balance)

IMPORTANT distinction used by services/orders.py to protect money:
  * ApiRejectedError — the provider DEFINITELY did not create the order
    (HTTP 4xx, or an explicit error in the body). Safe to refund the customer.
  * ApiClientError   — outcome UNKNOWN (timeout, network error, HTTP 5xx,
    garbled reply). The provider may have accepted the order, so we must NOT
    auto-refund or blindly retry — an admin is alerted instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class ApiClientError(Exception):
    """Generic failure — the result of the request may be unknown."""


class ApiRejectedError(ApiClientError):
    """The provider explicitly refused the request: nothing was created."""


# Normalized status values every client's get_status() maps into.
STATUS_COMPLETED = "completed"
STATUS_PROCESSING = "processing"
STATUS_FAILED = "failed"
STATUS_PARTIAL = "partial"


def _normalize_status(raw: str) -> str:
    raw = (raw or "").strip().lower().replace(" ", "_")
    if raw in {"completed", "complete", "success", "successful", "delivered", "done"}:
        return STATUS_COMPLETED
    if raw in {"partial", "partially_completed", "partial_refund"}:
        return STATUS_PARTIAL
    if raw in {"canceled", "cancelled", "failed", "fail", "failure", "refunded", "error", "rejected"}:
        return STATUS_FAILED
    return STATUS_PROCESSING  # pending, processing, in_progress, awaiting, ...


def _is_definitive_http_error(status: int) -> bool:
    return 400 <= status < 500 and status != 408


# ----------------------------------------------------------------------
# EPINBY — REST JSON API, header-based auth
# ----------------------------------------------------------------------
class EpinbyClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str | None = None, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, json_body: dict | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, headers=self._headers()) as http:
                async with http.request(method, url, json=json_body) as resp:
                    status = resp.status
                    text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ApiClientError(f"network error: {exc.__class__.__name__}") from exc

        error_cls = ApiRejectedError if _is_definitive_http_error(status) else ApiClientError
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise error_cls(f"Invalid response (HTTP {status}): {text[:200]}") from exc
        if not isinstance(data, dict):
            raise error_cls(f"Unexpected response (HTTP {status})")

        if status >= 400 or data.get("success") is False:
            err = data.get("error")
            message = err.get("message") if isinstance(err, dict) else (str(err) if err else f"HTTP {status}")
            raise (ApiRejectedError if status < 500 and status != 408 else ApiClientError)(message or f"HTTP {status}")
        return data

    async def get_balance(self) -> dict[str, Any]:
        result = await self._request("GET", "/getMe")
        return result.get("data", result)

    async def validate_player(self, product_id: str, player_id: str, server_id: str | None = None) -> dict[str, Any]:
        """Optional: validates a Player ID and returns the nickname (Epinby-specific)."""
        body: dict[str, Any] = {"product_id": product_id, "player_id": player_id}
        if server_id:
            body["server_id"] = server_id
        result = await self._request("POST", "/validate-player", body)
        return result.get("data", result)

    async def add_order(self, product_id: str, player_id: str, quantity: int = 1) -> str:
        body = {"product_id": product_id, "qty": quantity, "player_id": player_id}
        result = await self._request("POST", "/order", body)
        data = result.get("data", result)
        order_id = data.get("order_id") or data.get("id") if isinstance(data, dict) else None
        if not order_id:
            # HTTP 200 but no order id: the order MAY exist -> unknown outcome, not "rejected".
            raise ApiClientError(f"Unexpected order response: {str(result)[:200]}")
        return str(order_id)

    async def get_status(self, order_id: str) -> dict[str, Any]:
        result = await self._request("GET", f"/order/{order_id}")
        data = result.get("data", result)
        raw_status = str(data.get("status", "")) if isinstance(data, dict) else ""
        return {"status": _normalize_status(raw_status), "raw": raw_status}


# ----------------------------------------------------------------------
# GENERIC SMM PANEL — form-data, action=add/status/balance
# ----------------------------------------------------------------------
class SmmGenericClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str | None = None, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = {"key": self.api_key, **payload}
        if self.api_secret:
            data["secret"] = self.api_secret
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as http:
                async with http.post(self.base_url, data=data) as resp:
                    status = resp.status
                    text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise ApiClientError(f"network error: {exc.__class__.__name__}") from exc

        if status != 200:
            logger.warning("Provider API HTTP %s: %s", status, text[:300])
            raise (ApiRejectedError if _is_definitive_http_error(status) else ApiClientError)(f"HTTP {status}")
        try:
            result = json.loads(text)
        except ValueError as exc:
            raise ApiClientError(f"Invalid JSON response: {text[:200]}") from exc
        if not isinstance(result, dict):
            raise ApiClientError(f"Unexpected response: {text[:200]}")
        return result

    async def get_balance(self) -> dict[str, Any]:
        result = await self._post({"action": "balance"})
        if "error" in result:
            raise ApiClientError(str(result["error"]))
        return result

    async def add_order(self, service_id: str, player_id: str, quantity: int = 1) -> str:
        result = await self._post({"action": "add", "service": service_id, "link": player_id, "quantity": quantity})
        if "error" in result:
            raise ApiRejectedError(str(result["error"]))
        if "order" not in result:
            raise ApiClientError(f"Unexpected response: {str(result)[:200]}")
        return str(result["order"])

    async def get_status(self, order_id: str) -> dict[str, Any]:
        result = await self._post({"action": "status", "order": order_id})
        if "error" in result:
            raise ApiClientError(str(result["error"]))
        raw_status = str(result.get("status", ""))
        return {"status": _normalize_status(raw_status), "raw": raw_status}


PROVIDER_TYPES = {
    "EPINBY": "Epinby (epinby.com)",
    "SMM_GENERIC": "Generic SMM Panel (action=add/status)",
}


def get_provider_client(provider) -> EpinbyClient | SmmGenericClient:
    """provider: database.models.ApiProvider instance"""
    if provider.provider_type == "EPINBY":
        return EpinbyClient(provider.base_url, provider.api_key, provider.api_secret)
    return SmmGenericClient(provider.base_url, provider.api_key, provider.api_secret)

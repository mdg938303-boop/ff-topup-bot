"""
Mr Ai Pay (mraipay.top) automated bKash/Nagad/Rocket payment gateway client.

Security rule: the redirect to success_url is NEVER trusted by itself — anyone can
open that URL by hand. Every credit goes through verify_payment(), which asks the
gateway's own server for the authoritative status.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

CREATE_URL = "https://pay.mraipay.top/api/payment/create"
VERIFY_URL = "https://pay.mraipay.top/api/payment/verify"


class MrAiPayError(Exception):
    pass


@dataclass(frozen=True)
class PaymentLink:
    url: str
    transaction_id: str | None  # only known if the gateway tells us at creation time


def _extract_transaction_id(data: dict[str, Any], url: str) -> str | None:
    # NOTE: per the official docs the create-response only has status/message/payment_url — the
    # transaction id normally arrives later, as ?transactionId=… on the success redirect.
    for key in ("transaction_id", "transactionId"):
        value = data.get(key)
        if value:
            return str(value)
    query = parse_qs(urlsplit(url).query)
    for key in ("transaction_id", "transactionId"):
        if query.get(key):
            return query[key][0]
    return None


class MrAiPayClient:
    def __init__(self, api_key: str, secret_key: str, brand_key: str, timeout: int = 20) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.brand_key = brand_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "API-KEY": self.api_key,
            "SECRET-KEY": self.secret_key,
            "BRAND-KEY": self.brand_key,
        }

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, headers=self._headers()) as http:
                async with http.post(url, json=body) as resp:
                    text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise MrAiPayError(f"network error: {exc.__class__.__name__}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise MrAiPayError(f"Invalid response: {text[:200]}") from exc
        if not isinstance(data, dict):
            raise MrAiPayError("Unexpected response from gateway")
        return data

    async def create_payment(
        self, amount: Decimal, cus_name: str, cus_email: str, success_url: str, cancel_url: str,
        metadata: dict[str, Any] | None = None,
    ) -> PaymentLink:
        body: dict[str, Any] = {
            "cus_name": cus_name,
            "cus_email": cus_email,
            "success_url": success_url,
            "cancel_url": cancel_url,
            # Docs: "skip the trailing zeros in case the amount is a natural number" (10, 10.50, 10.6)
            "amount": format(amount.normalize(), "f"),
        }
        if metadata:
            # The official docs contradict themselves: the parameter table says "meta_data" but every
            # sample request uses "metadata". Sending both costs nothing and works either way.
            body["meta_data"] = metadata
            body["metadata"] = metadata

        data = await self._post(CREATE_URL, body)
        if not data.get("status"):
            raise MrAiPayError(str(data.get("message", "Payment creation failed")))
        payment_url = data.get("payment_url")
        if not payment_url:
            raise MrAiPayError("Unexpected response from gateway (no payment_url)")
        return PaymentLink(url=str(payment_url), transaction_id=_extract_transaction_id(data, str(payment_url)))

    async def verify_payment(self, transaction_id: str) -> dict[str, Any]:
        """Authoritative status from the gateway: {status: COMPLETED|PENDING|ERROR, amount, metadata, ...}"""
        data = await self._post(VERIFY_URL, {"transaction_id": transaction_id})
        if data.get("status") is False:
            raise MrAiPayError(str(data.get("message", "Verification failed")))
        return data

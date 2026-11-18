"""
HTTP routes served next to the Telegram webhook:
  GET /, /healthz       -> health check (Render)
  GET /payment/success  -> Mr Ai Pay redirect target (verified server-to-server before crediting)
  GET /payment/cancel
"""
from __future__ import annotations

import html
import logging
import re
import time
from collections import defaultdict, deque

from aiogram import Bot
from aiohttp import web

from database.database import get_session
from services.auto_deposit import deposit_success_text, verify_and_record
from services.mraipay_client import MrAiPayClient, MrAiPayError
from services.notify import notify_user
from services.payments import get_mraipay_config

logger = logging.getLogger(__name__)

_TXN_RE = re.compile(r"^[A-Za-z0-9_\-]{4,128}$")

# very small in-memory rate limiter: each hit triggers a verify call to the gateway
_RATE_LIMIT = 20          # requests
_RATE_WINDOW = 60         # per seconds, per client IP
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote) or "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        return True
    q.append(now)
    if len(_hits) > 5000:   # keep memory bounded
        for key in [k for k, v in _hits.items() if not v][:1000]:
            _hits.pop(key, None)
    return False


def _page(title: str, message: str, ok: bool, status: int = 200) -> web.Response:
    color = "#16a34a" if ok else "#dc2626"
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;
justify-content:center;min-height:100vh;margin:0;padding:16px}}.c{{background:#1e293b;padding:32px;border-radius:16px;
text-align:center;max-width:420px}}h1{{color:{color};font-size:1.4rem}}</style></head>
<body><div class="c"><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></div></body></html>"""
    return web.Response(text=body, content_type="text/html", status=status)


def register_web_routes(app: web.Application, bot: Bot) -> None:
    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def payment_cancel(_request: web.Request) -> web.Response:
        return _page("Payment Cancelled", "পেমেন্ট বাতিল করা হয়েছে। Telegram-এ ফিরে গিয়ে আবার চেষ্টা করতে পারেন।", ok=False)

    async def payment_success(request: web.Request) -> web.Response:
        if _rate_limited(_client_ip(request)):
            return _page("Too many requests", "একটু পরে আবার চেষ্টা করুন।", ok=False, status=429)

        transaction_id = (
            request.query.get("transactionId") or request.query.get("transaction_id") or ""
        ).strip()
        if not _TXN_RE.match(transaction_id):
            return _page("Invalid request", "Transaction ID পাওয়া যায়নি।", ok=False, status=400)

        try:
            async with get_session() as session:
                config = await get_mraipay_config(session)
                if config is None:
                    return _page("Unavailable", "Auto Deposit এই মুহূর্তে বন্ধ আছে।", ok=False, status=503)
                client = MrAiPayClient(config["api_key"], config["secret_key"], config["brand_key"])
                # NEVER trust the redirect itself: verify with the gateway, then credit idempotently.
                outcome = await verify_and_record(session, client, transaction_id)
        except MrAiPayError as exc:
            logger.warning("Payment verify failed for %s: %s", transaction_id, exc)
            return _page("Verification failed", "পেমেন্ট verify করা যায়নি। আপনার টাকা কাটা হয়ে থাকলে সাপোর্টে যোগাযোগ করুন।", ok=False, status=502)
        except Exception:  # noqa: BLE001
            logger.exception("Payment success handler crashed for %s", transaction_id)
            return _page("Error", "একটি সমস্যা হয়েছে। আপনার টাকা কাটা হয়ে থাকলে সাপোর্টে যোগাযোগ করুন।", ok=False, status=500)

        if outcome.user_id is None:
            return _page("Unknown payment", "এই পেমেন্টের সাথে কোনো অ্যাকাউন্ট মেলেনি। সাপোর্টে যোগাযোগ করুন।", ok=False, status=404)

        if outcome.status == "COMPLETED":
            if outcome.newly_credited:
                await notify_user(bot, outcome.user_id, deposit_success_text(outcome.amount))
            return _page("Payment Successful ✅", "আপনার ব্যালেন্স যোগ হয়েছে। Telegram-এ ফিরে যান।", ok=True)
        if outcome.status == "PENDING":
            return _page("Payment Pending ⏳", "পেমেন্ট এখনো confirm হয়নি। একটু পরে নিজে থেকেই ব্যালেন্স যোগ হবে।", ok=True)
        return _page("Payment Failed", "পেমেন্ট সম্পন্ন হয়নি।", ok=False)

    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/payment/success", payment_success)
    app.router.add_get("/payment/cancel", payment_cancel)

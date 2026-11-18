"""
Entry point. Auto-detects mode:
  - No PORT env var  -> POLLING   (local machine / VPS / Termux)
  - PORT env var set -> WEBHOOK    (Render — it injects PORT and RENDER_EXTERNAL_URL)

Also runs background jobs (APScheduler):
  * order status sync   — completes / auto-refunds API orders
  * payment reconcile   — credits Mr Ai Pay payments even if the user closed the browser
"""
import asyncio
import logging
import os
import signal
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import ADMIN_IDS, BOT_TOKEN, PUBLIC_BASE_URL, REDIS_URL, WEBHOOK_SECRET
from database.database import SessionLocal, dispose_db, init_db
from handlers import admin, admin_import, deposit, fallback, nav, orders, referral, user
from middlewares import BanMiddleware
from services.auto_deposit import reconcile_pending_intents
from services.catalog import price_sync_job
from services.notify import notify_admins
from services.order_sync import sync_all_processing_orders
from utils.crypto import encryption_enabled
from utils.helpers import esc
from web_routes import register_web_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/webhook"
PORT = os.getenv("PORT")
JOB_INTERVAL_SECONDS = 60
PRICE_SYNC_MINUTES = 15

_last_admin_error_alert = 0.0


async def build_storage() -> BaseStorage:
    """Redis keeps half-finished forms alive across restarts/deploys; falls back to memory."""
    if REDIS_URL:
        try:
            from datetime import timedelta

            from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage

            storage = RedisStorage.from_url(
                REDIS_URL, key_builder=DefaultKeyBuilder(with_destiny=True),
                state_ttl=timedelta(hours=24), data_ttl=timedelta(hours=24),
            )
            await storage.redis.ping()
            logger.info("FSM storage: Redis")
            return storage
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis unavailable (%s) — using in-memory FSM storage.", exc)
    logger.info("FSM storage: memory (forms reset on restart)")
    return MemoryStorage()


def create_dispatcher(storage: BaseStorage) -> Dispatcher:
    dp = Dispatcher(storage=storage)

    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())

    dp.include_router(nav.router)
    dp.include_router(admin.router)
    dp.include_router(admin_import.router)
    dp.include_router(deposit.router)
    dp.include_router(orders.router)
    dp.include_router(referral.router)
    dp.include_router(user.router)
    dp.include_router(fallback.router)   # must stay last

    @dp.errors()
    async def on_error(event: ErrorEvent, bot: Bot) -> bool:
        global _last_admin_error_alert
        exc = event.exception
        if "message is not modified" in str(exc):
            return True  # harmless: a button re-rendered identical content

        logger.error("Unhandled error while processing update", exc_info=exc)
        update = event.update
        try:
            if update.callback_query:
                await update.callback_query.answer("⚠️ একটা সমস্যা হয়েছে, আবার চেষ্টা করুন।", show_alert=True)
            elif update.message:
                await update.message.answer("⚠️ একটা সমস্যা হয়েছে। আবার চেষ্টা করুন, না হলে সাপোর্টে জানান।")
        except Exception:  # noqa: BLE001
            pass

        now = time.monotonic()
        if now - _last_admin_error_alert > 300:   # at most one admin alert / 5 min
            _last_admin_error_alert = now
            await notify_admins(bot, (
                f"🚨 <b>Bot error</b>\n<code>{esc(exc.__class__.__name__)}</code>: {esc(str(exc))[:300]}\n"
                "বিস্তারিত Render logs-এ আছে।"
            ))
        return True

    return dp


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sync_all_processing_orders, "interval", seconds=JOB_INTERVAL_SECONDS,
        args=[bot, SessionLocal], id="order_status_sync", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        reconcile_pending_intents, "interval", seconds=JOB_INTERVAL_SECONDS,
        args=[bot, SessionLocal], id="payment_reconcile", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        price_sync_job, "interval", minutes=PRICE_SYNC_MINUTES,
        args=[bot, SessionLocal], id="price_sync", max_instances=1, coalesce=True,
    )
    scheduler.start()
    return scheduler


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)   # Render sends SIGTERM on every deploy/restart
        except NotImplementedError:  # Windows
            pass


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PORT is set but no public URL: set RENDER_EXTERNAL_URL or WEBHOOK_BASE_URL.")

    app = web.Application()
    register_web_routes(app, bot)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(PORT)).start()

    webhook_url = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url, secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False,
    )
    logger.info("Webhook mode — listening on :%s, webhook=%s", PORT, webhook_url)

    stop = asyncio.Event()
    _install_stop_signals(stop)
    await stop.wait()
    logger.info("Shutdown signal received — stopping.")
    await runner.cleanup()


async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Polling mode (set PORT to run as a webhook server).")
    stop = asyncio.Event()
    _install_stop_signals(stop)
    polling = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False))
    waiter = asyncio.create_task(stop.wait())
    await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if not polling.done():
        await dp.stop_polling()
    await asyncio.gather(polling, return_exceptions=True)


async def main() -> None:
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty — nobody can open the Admin Panel!")
    if not encryption_enabled():
        logger.warning("SECRET_ENCRYPTION_KEY not set — provider/gateway keys are stored in PLAINTEXT.")

    await init_db()
    logger.info("Database ready.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = await build_storage()
    dp = create_dispatcher(storage)
    scheduler = start_scheduler(bot)

    try:
        if PORT:
            await run_webhook(bot, dp)
        else:
            await run_polling(bot, dp)
    finally:
        scheduler.shutdown(wait=False)
        await storage.close()
        await bot.session.close()
        await dispose_db()
        logger.info("Stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())

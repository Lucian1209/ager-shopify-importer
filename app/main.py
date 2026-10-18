from __future__ import annotations

import hmac
import logging
import threading
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .feedfile import download_feed
from .parser import iter_products_grouped
from .shopify import ShopifyClient, ShopifyError, normalize_location_id
from .store import FeedNotFound, FeedStore
from .sync import sync_products

VERSION = "1.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    AGER_FEED_URL: str = ""  # optional: seeds the first feed in an empty database
    SHOPIFY_SHOP: str = ""
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_API_VERSION: str = "2026-10"
    SHOPIFY_LOCATION_ID: str = ""
    MARKUP_PERCENT: Decimal = Decimal("0")  # markup of the seeded feed only; feeds carry their own markup
    MARKUP_FIXED: Decimal = Decimal("0")
    SYNC_INTERVAL_MINUTES: int = 360  # 0 disables the in-process scheduler
    SYNC_LIMIT: int = Field(default=10, ge=0)  # products per scheduled run and feed; 0 = the whole feed
    SYNC_DRY_RUN: bool = True
    SYNC_UPDATE_CONTENT: bool = True  # False: existing products only get price/stock/variants updated
    APP_API_KEY: str = ""
    FEED_DB_PATH: str = "/tmp/feeds.db"
    FEED_CACHE_DIR: str = "/tmp/importer-feeds"
    LOG_LEVEL: str = "INFO"


settings = Settings()
logging.basicConfig(level=settings.LOG_LEVEL.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app.main")

store = FeedStore(
    settings.FEED_DB_PATH,
    (
        ("Основний фід", settings.AGER_FEED_URL, str(settings.MARKUP_PERCENT), str(settings.MARKUP_FIXED))
        if settings.AGER_FEED_URL else None
    ),
)
_sync_lock = threading.Lock()
DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- scheduler
def _scheduled_loop(stop: threading.Event) -> None:
    # The first run waits for the configured interval: no surprise writes right after a deploy.
    interval = settings.SYNC_INTERVAL_MINUTES * 60
    while not stop.wait(interval):
        if settings.SYNC_DRY_RUN:
            continue
        try:
            run_scheduled_sync()
        except Exception:  # noqa: BLE001 - the scheduler must survive any failure
            log.exception("Scheduled sync crashed")


def run_scheduled_sync() -> None:
    feeds = store.list()
    if not feeds:
        return
    if not _sync_lock.acquire(blocking=False):
        log.info("Scheduled sync skipped: another sync is running")
        return
    shopify = None
    try:
        shopify = make_shopify_client()
        location_id = resolve_location(shopify)
        for feed in feeds:
            try:
                xml_path = ensure_feed(force=True, feed_id=feed["id"])
                results = sync_products(
                    shopify,
                    iter_products_grouped(xml_path, Decimal(feed["markup_percent"]), Decimal(feed["markup_fixed"])),
                    location_id, dry_run=False, limit=settings.SYNC_LIMIT, update_content=settings.SYNC_UPDATE_CONTENT,
                )
                summary: dict[str, int] = {}
                for r in results:
                    summary[r["action"]] = summary.get(r["action"], 0) + 1
                log.info("Scheduled sync of feed %r finished: %s", feed["name"], summary)
            except ShopifyError:
                raise  # credentials/API are broken: every other feed would fail the same way
            except Exception:  # noqa: BLE001 - one broken supplier feed must not block the others
                log.exception("Scheduled sync of feed %r failed", feed["name"])
    finally:
        if shopify is not None:
            shopify.close()
        _sync_lock.release()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.APP_API_KEY:
        log.warning("APP_API_KEY is not set: all protected endpoints are disabled (503)")
    stop = threading.Event()
    if settings.SYNC_INTERVAL_MINUTES > 0:
        threading.Thread(target=_scheduled_loop, args=(stop,), daemon=True, name="sync-scheduler").start()
    yield
    stop.set()


app = FastAPI(title="DROPT / AGER → Shopify Importer", version=VERSION, lifespan=lifespan)


# --------------------------------------------------------------------------- helpers
def authorize(key: Optional[str]) -> None:
    if not settings.APP_API_KEY:
        raise HTTPException(503, "APP_API_KEY is not configured; protected endpoint disabled")
    # Compare bytes: str comparison raises TypeError (HTTP 500) for non-ASCII input.
    if not key or not hmac.compare_digest(key.encode("utf-8"), settings.APP_API_KEY.encode("utf-8")):
        raise HTTPException(401, "Missing or invalid X-App-Key")


def get_feed(feed_id: int | None = None) -> dict:
    try:
        return store.get(feed_id)
    except FeedNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


def feed_path(feed_id: int) -> Path:
    return Path(settings.FEED_CACHE_DIR) / f"feed-{feed_id}.xml"


def ensure_feed(force: bool = False, feed_id: int | None = None) -> str:
    feed = get_feed(feed_id)
    path = feed_path(feed["id"])
    if path.exists() and path.stat().st_size > 0 and not force:
        return str(path)
    try:
        download_feed(feed["url"], path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feed %r download failed: %s", feed["name"], exc)
        raise HTTPException(502, f"Could not download feed '{feed['name']}': {exc}") from exc
    return str(path)


def feed_markup(feed_id: int | None):
    f = get_feed(feed_id)
    return f, Decimal(f["markup_percent"]), Decimal(f["markup_fixed"])


def make_shopify_client() -> ShopifyClient:
    return ShopifyClient(settings.SHOPIFY_SHOP, settings.SHOPIFY_CLIENT_ID, settings.SHOPIFY_CLIENT_SECRET, settings.SHOPIFY_API_VERSION)


def shopify_client() -> ShopifyClient:
    try:
        return make_shopify_client()
    except ShopifyError as exc:
        raise HTTPException(400, str(exc)) from exc


def resolve_location(shopify: ShopifyClient) -> str:
    if settings.SHOPIFY_LOCATION_ID:
        return normalize_location_id(settings.SHOPIFY_LOCATION_ID)
    locations = shopify.locations()
    if not locations:
        raise ShopifyError("No active Shopify locations found")
    return locations[0]["id"]


# --------------------------------------------------------------------------- models
class FeedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=3000)
    markup_percent: Decimal = Field(default=Decimal("0"), ge=Decimal("-99"), le=Decimal("1000"))
    markup_fixed: Decimal = Field(default=Decimal("0"), ge=Decimal("-1000000"), le=Decimal("1000000"))

    @field_validator("name", "url")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        return v

    @field_validator("url")
    @classmethod
    def _http_only(cls, v: str) -> str:
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("Feed URL must start with http:// or https://")
        return v


# --------------------------------------------------------------------------- routes
@app.get("/", include_in_schema=False)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": VERSION,
        "shopify_configured": bool(settings.SHOPIFY_SHOP and settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET),
        "dry_run": settings.SYNC_DRY_RUN,
    }


@app.get("/feeds")
def list_feeds(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    return {"feeds": store.list()}


@app.post("/feeds")
def create_feed(payload: FeedCreate, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    return {"feed": store.create(payload.name, payload.url, str(payload.markup_percent), str(payload.markup_fixed))}


@app.delete("/feeds/{feed_id}")
def delete_feed(feed_id: int, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    try:
        store.delete(feed_id)
    except FeedNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    feed_path(feed_id).unlink(missing_ok=True)
    return {"ok": True, "deleted": feed_id}


@app.get("/preview")
def preview(limit: int = Query(default=10, ge=1, le=100), feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    f, percent, fixed = feed_markup(feed_id)
    xml_path = ensure_feed(feed_id=f["id"])
    items = []
    for p in iter_products_grouped(xml_path, percent, fixed):
        if len(items) >= limit:
            break
        items.append(p.to_dict())
    return {"count": len(items), "products": items, "feed_id": f["id"], "feed_name": f["name"],
            "markup_percent": str(percent), "markup_fixed": str(fixed)}


_stats_cache: dict[tuple, dict] = {}


@app.get("/stats")
def stats(feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    f, percent, fixed = feed_markup(feed_id)
    xml_path = ensure_feed(feed_id=f["id"])
    # A full pass over a large feed takes seconds: cache per (feed, file version, markup).
    key = (f["id"], Path(xml_path).stat().st_mtime_ns, str(percent), str(fixed))
    if key not in _stats_cache:
        products = variants = stock = 0
        for p in iter_products_grouped(xml_path, percent, fixed):
            products += 1
            variants += len(p.variants)
            stock += sum(v.stock for v in p.variants)
        if len(_stats_cache) > 64:
            _stats_cache.clear()
        _stats_cache[key] = {"products": products, "variants": variants, "stock_units": stock}
    return {**_stats_cache[key], "feed_id": f["id"], "feed_name": f["name"],
            "markup_percent": str(percent), "markup_fixed": str(fixed)}


@app.post("/feed/refresh")
def refresh_feed(feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    f = get_feed(feed_id)
    path = ensure_feed(force=True, feed_id=f["id"])
    return {"ok": True, "bytes": Path(path).stat().st_size, "feed_id": f["id"]}


@app.get("/shopify/locations")
def shopify_locations(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    shopify = shopify_client()
    try:
        return {"locations": shopify.locations()}
    except ShopifyError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        shopify.close()


@app.post("/sync")
def sync(limit: int = Query(default=10, ge=1, le=100), offset: int = Query(default=0, ge=0),
         dry_run: Optional[bool] = None, feed_id: int | None = None,
         x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running")
    shopify = None
    try:
        f, percent, fixed = feed_markup(feed_id)
        xml_path = ensure_feed(feed_id=f["id"])
        effective_dry_run = settings.SYNC_DRY_RUN if dry_run is None else dry_run
        shopify = shopify_client()
        location_id = resolve_location(shopify)
        results = sync_products(shopify, iter_products_grouped(xml_path, percent, fixed), location_id,
                                effective_dry_run, limit=limit, offset=offset, update_content=settings.SYNC_UPDATE_CONTENT)
        return {"dry_run": effective_dry_run, "feed_id": f["id"], "feed_name": f["name"],
                "location_id": location_id, "offset": offset, "results": results}
    except ShopifyError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        if shopify is not None:
            shopify.close()
        _sync_lock.release()

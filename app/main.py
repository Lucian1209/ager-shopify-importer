from __future__ import annotations
import hmac
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic_settings import BaseSettings, SettingsConfigDict
from .parser import iter_products_grouped
from .shopify import ShopifyClient, ShopifyError
from .sync import sync_product

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    AGER_FEED_URL: str = "https://ager.ua/yml_prom/?code=multi&param=8261"
    AGER_XML_PATH: str = "/tmp/ager-feed.xml"
    SHOPIFY_SHOP: str = ""
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_API_VERSION: str = "2026-10"
    SHOPIFY_LOCATION_ID: str = ""
    MARKUP_PERCENT: Decimal = Decimal("0")
    MARKUP_FIXED: Decimal = Decimal("0")
    SYNC_INTERVAL_MINUTES: int = 360
    SYNC_LIMIT: int = 10
    SYNC_DRY_RUN: bool = True
    APP_API_KEY: str = ""

settings = Settings()
app = FastAPI(title="AGER → Shopify Importer", version="0.4.0")
_sync_lock = threading.Lock()


def authorize(key: Optional[str]):
    if not settings.APP_API_KEY:
        raise HTTPException(503, "APP_API_KEY is not configured; protected endpoint disabled")
    if not key or not hmac.compare_digest(key, settings.APP_API_KEY):
        raise HTTPException(401, "Missing or invalid X-App-Key")


def ensure_feed(force: bool = False) -> str:
    path = Path(settings.AGER_XML_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0 and not force:
        return str(path)
    if not settings.AGER_FEED_URL:
        raise HTTPException(400, "AGER_FEED_URL is not configured")
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with httpx.stream("GET", settings.AGER_FEED_URL, timeout=httpx.Timeout(120, connect=30), follow_redirects=True) as response:
            response.raise_for_status()
            with tmp.open("wb") as out:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    out.write(chunk)
        if tmp.stat().st_size < 1000:
            raise RuntimeError("Downloaded feed is unexpectedly small")
        tmp.replace(path)
    except Exception as exc:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(502, f"Could not download AGER feed: {exc}") from exc
    return str(path)


def client() -> ShopifyClient:
    try:
        return ShopifyClient(settings.SHOPIFY_SHOP, settings.SHOPIFY_CLIENT_ID, settings.SHOPIFY_CLIENT_SECRET, settings.SHOPIFY_API_VERSION)
    except ShopifyError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.get("/health")
def health():
    return {"ok": True, "shopify_configured": bool(settings.SHOPIFY_SHOP and settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET), "feed_cached": Path(settings.AGER_XML_PATH).exists(), "dry_run": settings.SYNC_DRY_RUN}

@app.get("/preview")
def preview(limit: int = Query(default=10, ge=1, le=100), x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    xml_path = ensure_feed()
    items=[]
    for i,p in enumerate(iter_products_grouped(xml_path, settings.MARKUP_PERCENT, settings.MARKUP_FIXED)):
        if i >= limit: break
        items.append(p.to_dict())
    return {"count": len(items), "products": items, "markup_percent": str(settings.MARKUP_PERCENT), "markup_fixed": str(settings.MARKUP_FIXED)}

@app.get("/stats")
def stats(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    xml_path = ensure_feed()
    products=0; variants=0; stock=0
    for p in iter_products_grouped(xml_path, settings.MARKUP_PERCENT, settings.MARKUP_FIXED):
        products += 1; variants += len(p.variants); stock += sum(v.stock for v in p.variants)
    return {"products": products, "variants": variants, "stock_units": stock}

@app.post("/feed/refresh")
def refresh_feed(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    path = ensure_feed(force=True)
    return {"ok": True, "bytes": Path(path).stat().st_size}

@app.get("/shopify/locations")
def shopify_locations(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    try:
        return {"locations": client().locations()}
    except ShopifyError as e:
        raise HTTPException(400, str(e)) from e

@app.post("/sync")
def sync(limit: int = Query(default=10, ge=1, le=100), dry_run: Optional[bool] = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running")
    try:
        xml_path = ensure_feed()
        effective_dry_run = settings.SYNC_DRY_RUN if dry_run is None else dry_run
        shopify = client()
        location_id = settings.SHOPIFY_LOCATION_ID
        if not location_id:
            locations = shopify.locations()
            if not locations:
                raise HTTPException(400, "No active Shopify locations found")
            location_id = locations[0]["id"]
        results=[]
        for i,p in enumerate(iter_products_grouped(xml_path, settings.MARKUP_PERCENT, settings.MARKUP_FIXED)):
            if i >= limit: break
            try:
                r=sync_product(shopify,p,location_id,effective_dry_run)
                results.append(r.__dict__)
            except Exception as e:
                results.append({"sku":p.source_sku,"action":"error","error":str(e)[:500]})
        return {"dry_run":effective_dry_run,"location_id":location_id,"results":results}
    finally:
        _sync_lock.release()


def _scheduled_loop():
    # First run waits for the configured interval; no surprise writes immediately after deploy.
    while True:
        time.sleep(max(5, settings.SYNC_INTERVAL_MINUTES * 60))
        if settings.SYNC_DRY_RUN:
            continue
        try:
            # Re-fetch supplier feed before scheduled sync.
            ensure_feed(force=True)
            # The same code path is called with a private in-process key.
            if not _sync_lock.acquire(blocking=False):
                continue
            try:
                shopify = client()
                location_id = settings.SHOPIFY_LOCATION_ID
                if not location_id:
                    locations = shopify.locations()
                    if not locations: continue
                    location_id = locations[0]["id"]
                for i,p in enumerate(iter_products_grouped(settings.AGER_XML_PATH, settings.MARKUP_PERCENT, settings.MARKUP_FIXED)):
                    if i >= settings.SYNC_LIMIT: break
                    try: sync_product(shopify, p, location_id, False)
                    except Exception: pass
            finally:
                _sync_lock.release()
        except Exception:
            # Health endpoint remains available; hosting logs can show the exception in production logging.
            pass

@app.on_event("startup")
def start_scheduler():
    if settings.SYNC_INTERVAL_MINUTES > 0:
        threading.Thread(target=_scheduled_loop, daemon=True, name="ager-sync-scheduler").start()

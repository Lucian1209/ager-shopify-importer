from __future__ import annotations
import hmac
import os
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse
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
app = FastAPI(title="AGER → Shopify Importer", version="0.5.0")
_sync_lock = threading.Lock()
security = HTTPBasic()


def authorize(key: Optional[str]):
    if not settings.APP_API_KEY:
        raise HTTPException(503, "APP_API_KEY is not configured; protected endpoint disabled")
    if not key or not hmac.compare_digest(key, settings.APP_API_KEY):
        raise HTTPException(401, "Missing or invalid X-App-Key")


def verify_dashboard_user(credentials: HTTPBasicCredentials = Depends(security)):
    """Захист веб-інтерфейсу за допомогою HTTP Basic Auth. Пароль — це APP_API_KEY."""
    if not settings.APP_API_KEY or not hmac.compare_digest(credentials.password, settings.APP_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невірний пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


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


@app.get("/", response_class=HTMLResponse)
def dashboard(username: str = Depends(verify_dashboard_user)):
    """Інтерактивний веб-дашборд з керуванням імпортом."""
    return f"""
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <title>AGER → Shopify Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {{ font-family: system-ui, -apple-system, sans-serif; padding: 20px; max-width: 700px; margin: auto; background: #f6f6f7; color: #202223; }}
            .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 15px; }}
            h2 {{ color: #008060; margin-top: 0; }}
            button {{ background: #008060; color: white; border: none; padding: 10px 15px; border-radius: 5px; cursor: pointer; font-weight: bold; width: 100%; margin-top: 10px; }}
            button:hover {{ background: #006e52; }}
            button.danger {{ background: #d82c0d; }}
            button.danger:hover {{ background: #b4230b; }}
            button.secondary {{ background: #6d7175; }}
            button.secondary:hover {{ background: #56595c; }}
            pre {{ background: #202124; color: #00ffcc; padding: 12px; border-radius: 5px; overflow-x: auto; font-size: 13px; max-height: 400px; }}
            .row {{ display: flex; gap: 10px; }}
            .row button {{ flex: 1; }}
            .info {{ font-size: 14px; color: #6d7175; margin-bottom: 10px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>📦 AGER → Shopify Importer</h2>
            <p class="info">Авторизовано успішно. Ви можете керувати синхронізацією товарів прямо звідси.</p>
        </div>

        <div class="card">
            <h3>Статус та аналітика</h3>
            <div class="row">
                <button class="secondary" onclick="callApi('/health', 'GET')">🩺 Health</button>
                <button class="secondary" onclick="callApi('/stats', 'GET')">📊 Статистика</button>
                <button class="secondary" onclick="callApi('/preview?limit=5', 'GET')"> 👀 Прев'ю (5)</button>
            </div>
        </div>
        
        <div class="card">
            <h3>Керування синхронізацією</h3>
            <p class="info">Ліміт товарів за один раз: <b>{settings.SYNC_LIMIT}</b></p>
            <button onclick="runSync(true)">🧪 Тестовий запуск (Dry Run)</button>
            <button class="danger" onclick="runSync(false)">🚀 Реальний імпорт у Shopify (Draft)</button>
        </div>

        <div class="card">
            <h3>Вивід відповіді:</h3>
            <pre id="output">Очікування дій...</pre>
        </div>

        <script>
            const apiKey = "{settings.APP_API_KEY}";

            async function callApi(endpoint, method = 'GET', body = null) {
                const out = document.getElementById('output');
                out.innerText = "Запит виконується...";
                try {
                    const options = {
                        method: method,
                        headers: {{ 'x-app-key': apiKey }}
                    };
                    if (body) {
                        options.headers['Content-Type'] = 'application/json';
                        options.body = JSON.stringify(body);
                    }
                    const res = await fetch(endpoint, options);
                    const data = await res.json();
                    out.innerText = JSON.stringify(data, null, 2);
                } catch (err) {
                    out.innerText = "Помилка: " + err.message;
                }
            }

            async function runSync(dryRun) {
                if (!dryRun) {
                    if (!confirm("Ви впевнені, що хочете запустити РЕАЛЬНИЙ імпорт у Shopify?")) return;
                }
                await callApi(`/sync?limit={settings.SYNC_LIMIT}&dry_run=${dryRun}`, 'POST');
            }
        </script>
    </body>
    </html>
    """


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

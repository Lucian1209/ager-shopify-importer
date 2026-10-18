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
app = FastAPI(title="DROPT / AGER → Shopify Importer", version="0.5.0")
_sync_lock = threading.Lock()

DASHBOARD_HTML = r"""<!doctype html>
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>DROPT / AGER → Shopify</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#131b2e;--line:#28334b;--txt:#edf2ff;--muted:#9ba9c4;--accent:#a78bfa;--green:#86efac;--red:#fca5a5}*{box-sizing:border-box}body{margin:0;background:linear-gradient(160deg,#111a31,var(--bg) 45%);color:var(--txt);font:16px system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}.wrap{max-width:900px;margin:auto;padding:22px 15px 48px}.head{display:flex;gap:12px;align-items:center;margin-bottom:22px}.logo{width:48px;height:48px;border-radius:15px;background:#29204a;display:grid;place-items:center;font-size:23px}.head h1{font-size:22px;margin:0}.sub{color:var(--muted);font-size:13px;margin-top:4px}.card{background:rgba(19,27,46,.96);border:1px solid var(--line);border-radius:18px;padding:17px;margin:13px 0;box-shadow:0 8px 28px #0002}.card h2{font-size:17px;margin:0 0 12px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:190px}input{width:100%;background:#0b1222;color:var(--txt);border:1px solid #394663;border-radius:10px;padding:12px;font-size:15px}button{background:#8b5cf6;color:white;border:0;border-radius:11px;padding:12px 15px;font-size:14px;font-weight:650;cursor:pointer}button.secondary{background:#25314b}button.warn{background:#9f3d50}.statgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stat{background:#0b1222;border:1px solid var(--line);border-radius:12px;padding:13px}.num{font-size:22px;font-weight:750}.label{font-size:12px;color:var(--muted);margin-top:3px}.status{font-size:13px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px}.good{color:var(--green)}.bad{color:var(--red)}.results{overflow:auto;margin-top:12px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.hint{font-size:12px;color:var(--muted);line-height:1.5}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}@media(max-width:520px){.statgrid{grid-template-columns:1fr 1fr}.stat:last-child{grid-column:span 2}.head h1{font-size:19px}.card{padding:14px}}
</style></head><body><main class="wrap">
<header class="head"><div class="logo">↗</div><div><h1>DROPT / AGER → Shopify</h1><div class="sub">Панель керування імпортом товарів</div></div></header>
<section class="card"><h2>🔐 Доступ до панелі</h2><div class="row"><div class="grow"><input id="key" type="password" autocomplete="current-password" placeholder="Встав APP_API_KEY з Render"></div><button onclick="saveKey()">Підключити</button></div><div class="hint" style="margin-top:9px">Ключ зберігається лише в цьому браузері. Не надсилай його в чат і не додавай у GitHub.</div><div id="status" class="status">Введи ключ доступу й натисни «Підключити».</div></section>
<section class="card"><h2>📊 Стан фіда</h2><div class="statgrid"><div class="stat"><div id="products" class="num">—</div><div class="label">Товарних карток</div></div><div class="stat"><div id="variants" class="num">—</div><div class="label">Варіантів</div></div><div class="stat"><div id="stock" class="num">—</div><div class="label">Одиниць залишку</div></div></div><div class="actions"><button class="secondary" onclick="loadStats()">Оновити статистику</button><button class="secondary" onclick="refreshFeed()">Завантажити свіжий фід</button></div></section>
<section class="card"><h2>🧪 Тестовий перегляд</h2><div class="row"><div class="grow"><label class="hint" for="limit">Кількість товарів (1–100)</label><input id="limit" type="number" min="1" max="100" value="10"></div><button onclick="preview()">Переглянути товари</button></div><div id="preview" class="results"></div></section>
<section class="card"><h2>🔄 Shopify — тестова синхронізація</h2><p class="hint">Безпечний тестовий запуск: показує, що було б створено або оновлено, але не записує зміни в Shopify. Товари залишаються чернетками.</p><div class="actions"><button onclick="drySync()">Запустити dry-run</button></div><div id="syncresult" class="results"></div></section>
<section class="card"><h2>⚙️ Налаштування</h2><div class="hint">Поточна націнка: <b id="markup">—</b>. Щоб змінити націнку, інтервал або режим запису, поки що відкрий Render → сервіс → Environment. Для першого запуску залишай <b>SYNC_DRY_RUN=true</b>. Не вимикай цей режим до перевірки результатів.</div></section>
<footer class="sub" style="text-align:center;padding:12px">DROPT / AGER Importer · керування через захищений API</footer></main>
<script>
const $=id=>document.getElementById(id);let apiKey='';try{apiKey=localStorage.getItem('ager_app_key')||'';$('key').value=apiKey}catch(e){}
function setStatus(msg,ok=true){$('status').textContent=msg;$('status').className='status '+(ok?'good':'bad')}
function saveKey(){apiKey=$('key').value.trim();if(!apiKey){setStatus('Встав APP_API_KEY з Render.',false);return}try{localStorage.setItem('ager_app_key',apiKey)}catch(e){};setStatus('Ключ збережено. Перевіряю підключення…');loadStats()}
async function req(path,method='GET'){if(!apiKey)throw Error('Спочатку введи APP_API_KEY і натисни «Підключити».');const r=await fetch(path,{method,headers:{'X-App-Key':apiKey}});let d;try{d=await r.json()}catch(e){d={detail:await r.text()}}if(!r.ok)throw Error(d.detail||('HTTP '+r.status));return d}
function err(e){setStatus(e.message||String(e),false)}
async function loadStats(){try{setStatus('Перевіряю підключення та читаю фід…');const [h,s]=await Promise.all([fetch('/health').then(r=>r.json()),req('/stats')]);$('products').textContent=Number(s.products).toLocaleString('uk-UA');$('variants').textContent=Number(s.variants).toLocaleString('uk-UA');$('stock').textContent=Number(s.stock_units).toLocaleString('uk-UA');$('markup').textContent=s.markup_percent+'% + '+s.markup_fixed+' грн';setStatus('Підключення працює. Dry-run: '+(h.dry_run?'увімкнено':'вимкнено')+'. Shopify: '+(h.shopify_configured?'налаштовано':'потрібно налаштувати ключі в Render')+'.')}catch(e){err(e)}}
async function refreshFeed(){try{setStatus('Завантажую актуальний XML-фід AGER. Це може зайняти кілька хвилин…');const d=await req('/feed/refresh','POST');setStatus('Фід оновлено. Розмір: '+Number(d.bytes).toLocaleString('uk-UA')+' байтів.');await loadStats()}catch(e){err(e)}}
async function preview(){try{const n=Math.max(1,Math.min(100,Number($('limit').value)||10));setStatus('Читаю перші '+n+' товарів…');const d=await req('/preview?limit='+n);const rows=d.products.map(p=>'<tr><td>'+esc(p.source_sku)+'</td><td>'+esc(p.title||p.name||'')+'</td><td>'+esc(p.price??'')+' грн</td><td>'+esc((p.variants||[]).length)+'</td></tr>').join('');$('preview').innerHTML='<table><thead><tr><th>SKU</th><th>Назва</th><th>Ціна</th><th>Варіанти</th></tr></thead><tbody>'+rows+'</tbody></table>';setStatus('Завантажено '+d.count+' товарів для перегляду.')}catch(e){err(e)}}
async function drySync(){try{const n=Math.max(1,Math.min(100,Number($('limit').value)||10));setStatus('Виконую тестову синхронізацію без запису в Shopify…');const d=await req('/sync?limit='+n+'&dry_run=true','POST');const rows=(d.results||[]).map(x=>'<tr><td>'+esc(x.sku||'')+'</td><td>'+esc(x.action||'')+'</td><td>'+esc(x.error||'')+'</td></tr>').join('');$('syncresult').innerHTML='<table><thead><tr><th>SKU</th><th>Дія</th><th>Повідомлення</th></tr></thead><tbody>'+rows+'</tbody></table>';setStatus('Dry-run завершено. Записів у Shopify не виконувалось. Переглянь результати нижче.')}catch(e){err(e)}}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
</script></body></html>"""

@app.get("/", include_in_schema=False)
def dashboard():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(DASHBOARD_HTML)


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
    return {"products": products, "variants": variants, "stock_units": stock, "markup_percent": str(settings.MARKUP_PERCENT), "markup_fixed": str(settings.MARKUP_FIXED)}

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

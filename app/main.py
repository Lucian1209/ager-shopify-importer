from __future__ import annotations
import hmac
import os
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
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
<html lang="uk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="dark"><title>Feed → Shopify</title>
<style>:root{color-scheme:dark;--bg:#0b1020;--card:#131b2e;--line:#28334b;--txt:#edf2ff;--muted:#9ba9c4;--accent:#a78bfa;--green:#86efac;--red:#fca5a5}*{box-sizing:border-box}body{margin:0;background:linear-gradient(160deg,#111a31,var(--bg) 45%);color:var(--txt);font:16px system-ui,sans-serif;min-height:100vh}.wrap{max-width:1000px;margin:auto;padding:22px 15px 48px}.head{display:flex;gap:12px;align-items:center;margin-bottom:22px}.logo{width:48px;height:48px;border-radius:15px;background:#29204a;display:grid;place-items:center;font-size:23px}.head h1{font-size:22px;margin:0}.sub,.hint{color:var(--muted);font-size:13px;line-height:1.5}.card{background:rgba(19,27,46,.96);border:1px solid var(--line);border-radius:18px;padding:17px;margin:13px 0}.card h2{font-size:17px;margin:0 0 12px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.grow{flex:1;min-width:190px}input,select{width:100%;background:#0b1222;color:var(--txt);border:1px solid #394663;border-radius:10px;padding:12px;font-size:15px}button{background:#8b5cf6;color:white;border:0;border-radius:11px;padding:12px 15px;font-size:14px;font-weight:650;cursor:pointer}button.secondary{background:#25314b}button.warn{background:#9f3d50}.statgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stat{background:#0b1222;border:1px solid var(--line);border-radius:12px;padding:13px}.num{font-size:22px;font-weight:750}.label{font-size:12px;color:var(--muted);margin-top:3px}.status{font-size:13px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px}.good{color:var(--green)}.bad{color:var(--red)}.results{overflow:auto;margin-top:12px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}.feeditem{border:1px solid var(--line);border-radius:12px;padding:12px;margin:8px 0;display:flex;gap:10px;align-items:center;justify-content:space-between}.feeditem.active{border-color:var(--accent);background:#8b5cf61a}.feedmeta{min-width:0;overflow-wrap:anywhere}.feedname{font-weight:700}.feedurl{font-size:12px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}@media(max-width:520px){.statgrid{grid-template-columns:1fr 1fr}.stat:last-child{grid-column:span 2}.head h1{font-size:19px}.card{padding:14px}.feeditem{align-items:flex-start;flex-direction:column}}</style></head><body><main class="wrap">
<header class="head"><div class="logo">↗</div><div><h1>Feed → Shopify</h1><div class="sub">Кілька постачальників і фідів в одній панелі</div></div></header>
<section class="card"><h2>🔐 Доступ до панелі</h2><div class="row"><div class="grow"><input id="key" type="password" autocomplete="current-password" placeholder="Встав APP_API_KEY з Render"></div><button onclick="saveKey()">Підключити</button></div><div class="hint" style="margin-top:9px">Ключ зберігається лише в цьому браузері. Не надсилай його в чат і не додавай у GitHub.</div><div id="status" class="status">Введи ключ доступу й натисни «Підключити».</div></section>
<section class="card"><h2>➕ Додати фід</h2><div class="row"><div class="grow"><label class="hint">Назва для себе</label><input id="feedname" placeholder="Наприклад, DROPT одяг"></div><div class="grow"><label class="hint">Повна URL-адреса XML-фіда</label><input id="feedurl" placeholder="https://example.com/feed.xml" inputmode="url"></div></div><div class="row" style="margin-top:10px"><div style="width:150px"><label class="hint">Націнка, %</label><input id="feedmarkup" type="number" step="0.1" value="0"></div><div style="width:170px"><label class="hint">Фіксована націнка, грн</label><input id="feedfixed" type="number" step="0.01" value="0"></div><button onclick="addFeed()">Додати фід</button></div><p class="hint">Можна додавати скільки потрібно фідів. Для URL не потрібен фрагмент після символу #. Новий фід спершу лише перевіряємо; автоматичного запису в Shopify ця дія не запускає.</p><div id="feeds"></div></section>
<section class="card"><h2>📊 Обраний фід: <span id="selectedname">—</span></h2><div class="statgrid"><div class="stat"><div id="products" class="num">—</div><div class="label">Товарних карток</div></div><div class="stat"><div id="variants" class="num">—</div><div class="label">Варіантів</div></div><div class="stat"><div id="stock" class="num">—</div><div class="label">Одиниць залишку</div></div></div><div class="actions"><button class="secondary" onclick="loadStats()">Оновити статистику</button><button class="secondary" onclick="refreshFeed()">Завантажити свіжий XML</button><button class="warn" onclick="deleteFeed()">Видалити обраний фід</button></div></section>
<section class="card"><h2>🧪 Тестовий перегляд</h2><div class="row"><div style="width:160px"><label class="hint">Кількість товарів (1–100)</label><input id="limit" type="number" min="1" max="100" value="10"></div><button onclick="preview()">Переглянути товари</button></div><div id="preview" class="results"></div></section>
<section class="card"><h2>🔄 Shopify — тестова синхронізація</h2><p class="hint">Спочатку використовуй лише dry-run: він не змінює Shopify. Кожен фід тестується окремо. Товари мають залишатися чернетками.</p><div class="actions"><button onclick="drySync()">Запустити dry-run для обраного фіда</button></div><div id="syncresult" class="results"></div></section>
<footer class="sub" style="text-align:center;padding:12px">Feed Importer · захищений API</footer></main>
<script>
const $=id=>document.getElementById(id);let apiKey='',feeds=[],selected='';try{apiKey=localStorage.getItem('ager_app_key')||'';$('key').value=apiKey;selected=localStorage.getItem('ager_selected_feed')||''}catch(e){}
function setStatus(msg,ok=true){$('status').textContent=msg;$('status').className='status '+(ok?'good':'bad')}function err(e){setStatus(e.message||String(e),false)}
function saveKey(){apiKey=$('key').value.trim();if(!apiKey){setStatus('Встав APP_API_KEY з Render.',false);return}try{localStorage.setItem('ager_app_key',apiKey)}catch(e){};setStatus('Ключ збережено. Завантажую список фідів…');loadFeeds()}
async function req(path,method='GET',body){if(!apiKey)throw Error('Спочатку введи APP_API_KEY і натисни «Підключити».');const opt={method,headers:{'X-App-Key':apiKey}};if(body){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}const r=await fetch(path,opt);let d;try{d=await r.json()}catch(e){d={detail:await r.text()}}if(!r.ok)throw Error(d.detail||('HTTP '+r.status));return d}
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadFeeds(){try{feeds=(await req('/feeds')).feeds||[];if(!feeds.length){$('feeds').innerHTML='<p class="hint">Фідів поки немає. Додай перший вище.</p>';return}if(!feeds.some(f=>String(f.id)===String(selected)))selected=String(feeds[0].id);try{localStorage.setItem('ager_selected_feed',selected)}catch(e){};$('feeds').innerHTML=feeds.map(f=>`<div class="feeditem ${String(f.id)===String(selected)?'active':''}"><div class="feedmeta"><div class="feedname">${esc(f.name)} ${String(f.id)===String(selected)?' · обраний':''}</div><div class="feedurl">${esc(f.url)}<br>Націнка: ${esc(f.markup_percent)}% + ${esc(f.markup_fixed)} грн</div></div><button class="secondary" onclick="chooseFeed('${f.id}')">Обрати</button></div>`).join('');const f=feeds.find(x=>String(x.id)===String(selected));$('selectedname').textContent=f?f.name:'—';await loadStats()}catch(e){err(e)}}
async function chooseFeed(id){selected=String(id);try{localStorage.setItem('ager_selected_feed',selected)}catch(e){};await loadFeeds()}
async function addFeed(){try{const name=$('feedname').value.trim(),url=$('feedurl').value.trim();if(!name||!url)throw Error('Вкажи назву та URL фіда.');if(!/^https?:\/\//i.test(url))throw Error('URL має починатися з https:// або http://');const d=await req('/feeds','POST',{name,url,markup_percent:Number($('feedmarkup').value||0),markup_fixed:Number($('feedfixed').value||0)});selected=String(d.feed.id);$('feedname').value='';$('feedurl').value='';setStatus('Фід додано. Натисни «Завантажити свіжий XML», щоб перевірити його.');await loadFeeds()}catch(e){err(e)}}
async function deleteFeed(){if(!selected)return;try{await req('/feeds/'+selected,'DELETE');selected='';$('preview').innerHTML='';setStatus('Фід видалено зі списку.');await loadFeeds()}catch(e){err(e)}}
async function loadStats(){if(!selected)return;try{setStatus('Читаю статистику обраного фіда…');const s=await req('/stats?feed_id='+encodeURIComponent(selected));$('products').textContent=Number(s.products).toLocaleString('uk-UA');$('variants').textContent=Number(s.variants).toLocaleString('uk-UA');$('stock').textContent=Number(s.stock_units).toLocaleString('uk-UA');setStatus('Статистика оновлена для «'+$('selectedname').textContent+'». Dry-run увімкнено за замовчуванням.')}catch(e){err(e)}}
async function refreshFeed(){if(!selected)return;try{setStatus('Завантажую XML обраного фіда… це може тривати кілька хвилин.');const d=await req('/feed/refresh?feed_id='+encodeURIComponent(selected),'POST');setStatus('Фід завантажено. Розмір: '+Number(d.bytes).toLocaleString('uk-UA')+' байтів.');await loadStats()}catch(e){err(e)}}
async function preview(){if(!selected)return;try{const n=Math.max(1,Math.min(100,Number($('limit').value)||10));setStatus('Читаю товари обраного фіда…');const d=await req('/preview?feed_id='+encodeURIComponent(selected)+'&limit='+n);const rows=d.products.map(p=>'<tr><td>'+esc(p.source_sku)+'</td><td>'+esc(p.title||p.name||'')+'</td><td>'+esc(p.price??'')+' грн</td><td>'+esc((p.variants||[]).length)+'</td><td>'+esc(p.available?'є':'немає')+'</td></tr>').join('');$('preview').innerHTML='<table><thead><tr><th>SKU</th><th>Назва</th><th>Ціна</th><th>Варіанти</th><th>Наявність</th></tr></thead><tbody>'+rows+'</tbody></table>';setStatus('Завантажено '+d.count+' товарів для перегляду.')}catch(e){err(e)}}
async function drySync(){if(!selected)return;try{const n=Math.max(1,Math.min(100,Number($('limit').value)||10));setStatus('Dry-run: перевіряю обраний фід без запису в Shopify…');const d=await req('/sync?feed_id='+encodeURIComponent(selected)+'&limit='+n+'&dry_run=true','POST');const rows=(d.results||[]).map(x=>'<tr><td>'+esc(x.sku||'')+'</td><td>'+esc(x.action||'')+'</td><td>'+esc(x.error||'')+'</td></tr>').join('');$('syncresult').innerHTML='<table><thead><tr><th>SKU</th><th>Дія</th><th>Повідомлення</th></tr></thead><tbody>'+rows+'</tbody></table>';setStatus('Dry-run завершено. Записів у Shopify не виконувалось.')}catch(e){err(e)}}
if(apiKey)loadFeeds();
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


FEED_DB_PATH = os.getenv("FEED_DB_PATH", "/tmp/feeds.db")
FEED_CACHE_DIR = os.getenv("FEED_CACHE_DIR", "/tmp/importer-feeds")


def db():
    Path(FEED_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(FEED_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
        markup_percent TEXT NOT NULL DEFAULT '0', markup_fixed TEXT NOT NULL DEFAULT '0',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    if conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 0 and settings.AGER_FEED_URL:
        conn.execute("INSERT INTO feeds(name,url,markup_percent,markup_fixed) VALUES(?,?,?,?)",
                     ("Основний фід", settings.AGER_FEED_URL, str(settings.MARKUP_PERCENT), str(settings.MARKUP_FIXED)))
        conn.commit()
    return conn


def get_feed(feed_id: int | None = None):
    conn = db()
    try:
        if feed_id is None:
            row = conn.execute("SELECT * FROM feeds ORDER BY id LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM feeds WHERE id=?", (feed_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Feed not found. Add a feed in the dashboard first.")
        return dict(row)
    finally:
        conn.close()


def feed_path(feed_id: int) -> Path:
    Path(FEED_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    return Path(FEED_CACHE_DIR) / f"feed-{feed_id}.xml"


def ensure_feed(force: bool = False, feed_id: int | None = None) -> str:
    feed = get_feed(feed_id)
    path = feed_path(feed["id"])
    if path.exists() and path.stat().st_size > 0 and not force:
        return str(path)
    tmp = path.with_suffix(path.suffix + ".part")
    try:
        with httpx.stream("GET", feed["url"], timeout=httpx.Timeout(180, connect=30), follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 FeedImporter/1.0"}) as response:
            response.raise_for_status()
            with tmp.open("wb") as out:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    out.write(chunk)
        if tmp.stat().st_size < 1000:
            raise RuntimeError("Downloaded feed is unexpectedly small")
        # Validate XML before replacing a previously valid cached feed.
        from lxml import etree
        etree.iterparse(str(tmp), events=("start",), recover=False, huge_tree=True).__next__()
        tmp.replace(path)
    except Exception as exc:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise HTTPException(502, f"Could not download feed '{feed['name']}': {exc}") from exc
    return str(path)


def feed_markup(feed_id: int | None):
    f = get_feed(feed_id)
    return f, Decimal(f["markup_percent"]), Decimal(f["markup_fixed"])

def client() -> ShopifyClient:
    try:
        return ShopifyClient(settings.SHOPIFY_SHOP, settings.SHOPIFY_CLIENT_ID, settings.SHOPIFY_CLIENT_SECRET, settings.SHOPIFY_API_VERSION)
    except ShopifyError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.get("/health")
def health():
    return {"ok": True, "shopify_configured": bool(settings.SHOPIFY_SHOP and settings.SHOPIFY_CLIENT_ID and settings.SHOPIFY_CLIENT_SECRET), "dry_run": settings.SYNC_DRY_RUN}


class FeedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=8, max_length=3000)
    markup_percent: Decimal = Decimal("0")
    markup_fixed: Decimal = Decimal("0")


@app.get("/feeds")
def list_feeds(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    conn = db()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM feeds ORDER BY id")]
        return {"feeds": rows}
    finally:
        conn.close()


@app.post("/feeds")
def create_feed(payload: FeedCreate, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    if not payload.url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Feed URL must start with http:// or https://")
    conn = db()
    try:
        cur = conn.execute("INSERT INTO feeds(name,url,markup_percent,markup_fixed) VALUES(?,?,?,?)",
                           (payload.name.strip(), payload.url.strip(), str(payload.markup_percent), str(payload.markup_fixed)))
        conn.commit()
        row = conn.execute("SELECT * FROM feeds WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"feed": dict(row)}
    finally:
        conn.close()


@app.delete("/feeds/{feed_id}")
def delete_feed(feed_id: int, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    conn = db()
    try:
        cur = conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "Feed not found")
    finally:
        conn.close()
    try: feed_path(feed_id).unlink(missing_ok=True)
    except Exception: pass
    return {"ok": True, "deleted": feed_id}


@app.get("/preview")
def preview(limit: int = Query(default=10, ge=1, le=100), feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    f, percent, fixed = feed_markup(feed_id)
    xml_path = ensure_feed(feed_id=f["id"])
    items=[]
    for i,p in enumerate(iter_products_grouped(xml_path, percent, fixed)):
        if i >= limit: break
        items.append(p.to_dict())
    return {"count": len(items), "products": items, "feed_id": f["id"], "feed_name": f["name"], "markup_percent": str(percent), "markup_fixed": str(fixed)}


@app.get("/stats")
def stats(feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    f, percent, fixed = feed_markup(feed_id)
    xml_path = ensure_feed(feed_id=f["id"])
    products=0; variants=0; stock=0
    for p in iter_products_grouped(xml_path, percent, fixed):
        products += 1; variants += len(p.variants); stock += sum(v.stock for v in p.variants)
    return {"products": products, "variants": variants, "stock_units": stock, "feed_id": f["id"], "feed_name": f["name"], "markup_percent": str(percent), "markup_fixed": str(fixed)}


@app.post("/feed/refresh")
def refresh_feed(feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    path = ensure_feed(force=True, feed_id=feed_id)
    return {"ok": True, "bytes": Path(path).stat().st_size, "feed_id": feed_id or get_feed()["id"]}

@app.get("/shopify/locations")
def shopify_locations(x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    try:
        return {"locations": client().locations()}
    except ShopifyError as e:
        raise HTTPException(400, str(e)) from e

@app.post("/sync")
def sync(limit: int = Query(default=10, ge=1, le=100), dry_run: Optional[bool] = None, feed_id: int | None = None, x_app_key: Optional[str] = Header(default=None)):
    authorize(x_app_key)
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, "A sync is already running")
    try:
        f, percent, fixed = feed_markup(feed_id)
        xml_path = ensure_feed(feed_id=f["id"])
        effective_dry_run = settings.SYNC_DRY_RUN if dry_run is None else dry_run
        shopify = client()
        location_id = settings.SHOPIFY_LOCATION_ID
        if not location_id:
            locations = shopify.locations()
            if not locations:
                raise HTTPException(400, "No active Shopify locations found")
            location_id = locations[0]["id"]
        results=[]
        for i,p in enumerate(iter_products_grouped(xml_path, percent, fixed)):
            if i >= limit: break
            try:
                r=sync_product(shopify,p,location_id,effective_dry_run)
                results.append(r.__dict__)
            except Exception as e:
                results.append({"sku":p.source_sku,"action":"error","error":str(e)[:500]})
        return {"dry_run":effective_dry_run,"feed_id":f["id"],"feed_name":f["name"],"location_id":location_id,"results":results}
    finally:
        _sync_lock.release()


def _scheduled_loop():
    # First run waits for the configured interval; no surprise writes immediately after deploy.
    while True:
        time.sleep(max(5, settings.SYNC_INTERVAL_MINUTES * 60))
        if settings.SYNC_DRY_RUN:
            continue
        try:
            conn = db()
            try:
                feed_rows = [dict(r) for r in conn.execute("SELECT * FROM feeds ORDER BY id")]
            finally:
                conn.close()
            if not feed_rows or not _sync_lock.acquire(blocking=False):
                continue
            try:
                shopify = client()
                location_id = settings.SHOPIFY_LOCATION_ID
                if not location_id:
                    locations = shopify.locations()
                    if not locations:
                        continue
                    location_id = locations[0]["id"]
                for feed in feed_rows:
                    try:
                        xml_path = ensure_feed(force=True, feed_id=feed["id"])
                        percent = Decimal(feed["markup_percent"])
                        fixed = Decimal(feed["markup_fixed"])
                        for i, product in enumerate(iter_products_grouped(xml_path, percent, fixed)):
                            if i >= settings.SYNC_LIMIT:
                                break
                            try:
                                sync_product(shopify, product, location_id, False)
                            except Exception:
                                pass
                    except Exception:
                        # One broken supplier feed must not block the remaining feeds.
                        continue
            finally:
                _sync_lock.release()
        except Exception:
            # Health endpoint remains available; hosting logs can show the exception in production logging.
            pass

@app.on_event("startup")
def start_scheduler():
    if settings.SYNC_INTERVAL_MINUTES > 0:
        threading.Thread(target=_scheduled_loop, daemon=True, name="ager-sync-scheduler").start()

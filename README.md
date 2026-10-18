# Feed → Shopify Importer v1.0

A small FastAPI/Docker service that downloads supplier XML feeds (DROPT / Prom YML and AGER multi-offer
feeds), previews the products with your markup applied and synchronises them to Shopify with the
`productSet` mutation. A mobile-friendly dashboard is served at `/`.

* **Prom / YML** (`<yml_catalog>`): every `<offer>` is one Shopify product; SKU = offer `id` (fallback `model`).
* **AGER**: consecutive `<offer>`s with the same `<sku>` / `group_id` are variants (option "Розмір") of one product.
  Offers of one group must be adjacent in the file.

## Safety model

| Rule | Detail |
|---|---|
| Dry-run by default | `SYNC_DRY_RUN=true`: scheduled sync never writes, and `POST /sync` previews unless you pass `dry_run=false`. |
| New products are `DRAFT` | Existing products keep their status and handle — a product you published is **not** reverted to draft. |
| Images | Uploaded only when the product has no media yet (no re-upload on every sync). |
| No free products | Variants whose price is missing/invalid/zero are skipped, never created at 0.00. |
| Loose matches are never updated | Products are matched by handle, then by exact SKU (or `SKU-<size>`) only. |
| Secrets | `SHOPIFY_CLIENT_SECRET` and `APP_API_KEY` live in environment variables only. |

## Deploy to Render

1. Push the **contents** of this folder to a private GitHub repository (`render.yaml` must be in the root).
2. Render → **New → Blueprint** → select the repository → apply.
3. Enter `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` (Dev Dashboard app with `read_products`, `write_products`,
   `read_inventory`, `write_inventory`, `read_locations`). `APP_API_KEY` is generated for you.
4. Open the service URL, paste `APP_API_KEY`, add/select a feed, click **Завантажити свіжий XML**, **Переглянути товари**
   and **Запустити dry-run**.
5. Only after the dry-run looks right, do the first write: `POST /sync?limit=10&dry_run=false` (see below).

### Hosting notes

* The free plan sleeps when idle, so the in-process scheduler is **not reliable there**. Use an always-on plan for
  unattended syncs, or keep syncing manually.
* The feed list (SQLite) and XML cache live in `/tmp` by default and are lost on redeploy. On a paid plan attach a disk
  and set `FEED_DB_PATH=/var/data/feeds.db`, `FEED_CACHE_DIR=/var/data/feed-cache` (commented example in `render.yaml`).
* Run a **single** worker/instance: the sync lock and the scheduler are in-process.
* SKUs must be unique across feeds — two feeds with the same SKU would overwrite the same Shopify product.

## API

`GET /` (dashboard) and `GET /health` are public; everything else needs the header `X-App-Key: <APP_API_KEY>`.

```bash
H='X-App-Key: YOUR_APP_API_KEY'; B=https://YOUR-SERVICE.onrender.com
curl -H "$H" $B/feeds
curl -H "$H" "$B/preview?feed_id=1&limit=10"
curl -H "$H" "$B/stats?feed_id=1"
curl -X POST -H "$H" "$B/feed/refresh?feed_id=1"
curl -H "$H" $B/shopify/locations
curl -X POST -H "$H" "$B/sync?feed_id=1&limit=10"                       # dry-run (default)
curl -X POST -H "$H" "$B/sync?feed_id=1&limit=100&offset=100&dry_run=false"  # real write, products 101–200
```

`/sync` handles at most 100 products per call (`limit`), use `offset` to walk through a big feed. The scheduler
handles `SYNC_LIMIT` products per feed per run (`0` = the whole feed).

## Configuration

See `.env.example` for every variable. Required: `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, `APP_API_KEY`.
Markup is configured **per feed** in the dashboard (`MARKUP_PERCENT` / `MARKUP_FIXED` only seed the first feed).
`SYNC_UPDATE_CONTENT=false` makes syncs touch only price, stock and variants of existing products.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the values
uvicorn app.main:app --reload
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

The parser, sync, Shopify-client and storage tests need only `lxml` (+ `httpx` for import); `tests/test_api.py`
runs when FastAPI is installed.

## Layout

```
app/main.py       FastAPI app, scheduler, endpoints
app/dashboard.html  dashboard UI
app/parser.py     streaming XML parser (Prom/YML and AGER)
app/sync.py       product → productSet input, handles, sync loop
app/shopify.py    GraphQL client (auth, retries, throttling)
app/store.py      SQLite feed store
app/feedfile.py   feed download + validation
```

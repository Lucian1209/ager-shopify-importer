# AGER → Shopify Importer v0.6

Includes a mobile-friendly web dashboard at the service root (`/`) for feed stats, feed refresh, product preview, and safe dry-run sync. The dashboard requires the generated `APP_API_KEY`. Markup and deployment secrets are still managed through Render Environment settings.

A small FastAPI/Docker service that downloads the AGER multi-language XML feed, groups offers by `sku`, previews products and can synchronize a limited batch to Shopify. Product status is set to `DRAFT`.

## Safety defaults

- `SYNC_DRY_RUN=true` — `/sync` previews create/update actions without writing to Shopify.
- `SYNC_LIMIT=10` — maximum products per scheduled run; manual `/sync` request also has a hard limit of 100.
- `MARKUP_PERCENT=0`, `MARKUP_FIXED=0` — no markup until deliberately configured.
- Shopify credentials and `APP_API_KEY` are environment variables, not stored in this repository.
- A loose SKU search will not be used to select an unrelated product for updating.

## Deploy to Render

1. Create a private GitHub repository, for example `ager-shopify-importer`.
2. Upload the *contents* of this project folder to the repository root (the `render.yaml` file must be at the root), then commit/push to the default branch.
3. Open Render and choose **New → Blueprint**, then connect the repository containing `render.yaml`.
4. Review the service and apply the Blueprint. The Blueprint generates `APP_API_KEY`; enter `SHOPIFY_CLIENT_ID` and `SHOPIFY_CLIENT_SECRET` in Render's environment settings. Never commit either secret.
5. Wait for the deployment to finish and open the service URL.
6. Open the service root URL (`https://YOUR-SERVICE.onrender.com/`) for the mobile dashboard. Paste the `APP_API_KEY` from Render Environment.
7. Keep `SYNC_DRY_RUN=true` until the preview and dry-run results have been checked.

Blueprint file: `render.yaml`.

## Important Render hosting note

The Blueprint uses Render's free web-service plan for initial testing. Free web services can spin down when idle, so an in-process scheduler is **not reliable for unattended automatic synchronization** on that plan. For dependable 6-hour synchronization, upgrade the web service to an always-on paid instance or move scheduled sync into a separate Render Cron Job/Worker before enabling writes. Do not change `SYNC_DRY_RUN` to `false` until the manual test is reviewed.

## Protected endpoints

All protected endpoints require the `X-App-Key` header. Use the generated `APP_API_KEY` value from Render's environment settings.

```bash
curl -H 'X-App-Key: YOUR_APP_API_KEY' https://YOUR-SERVICE.onrender.com/health
curl -H 'X-App-Key: YOUR_APP_API_KEY' 'https://YOUR-SERVICE.onrender.com/preview?limit=10'
curl -H 'X-App-Key: YOUR_APP_API_KEY' 'https://YOUR-SERVICE.onrender.com/stats'
curl -X POST -H 'X-App-Key: YOUR_APP_API_KEY' 'https://YOUR-SERVICE.onrender.com/sync?limit=10'
```

The initial `/sync` call is a dry run by default. Only after verifying its output should you make the first write with `POST /sync?limit=10&dry_run=false`. That explicit override writes to Shopify, but products remain `DRAFT`.

## Environment variables

Required secrets: `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET`, generated `APP_API_KEY`.

Other settings are declared in `render.yaml`: `SHOPIFY_SHOP`, `SHOPIFY_API_VERSION`, `AGER_FEED_URL`, `MARKUP_PERCENT`, `MARKUP_FIXED`, `SYNC_DRY_RUN`, `SYNC_LIMIT`, `SYNC_INTERVAL_MINUTES`, `AGER_XML_PATH`.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```


## DROPT / Prom YML feed support (v7)

This version supports both AGER's grouped offer structure and Prom/YML feeds with `<yml_catalog><shop><offers><offer>`. For DROPT, each offer is imported as a separate Shopify product; SKU is taken from the offer `id` (fallback: `model`). The parser reads `name_ua`/`name`, `description`, `vendor`, `model`, `url`, all `picture` entries, `price`, and `quantity_in_stock`. When `available="false"`, inventory is forced to zero even if a stock field is inconsistent. The dashboard preview includes the calculated price.

### Deploy / update on Render

1. Replace the repository contents with the contents of this ZIP (keep `render.yaml` in the repository root), or copy the updated `app/parser.py`, `app/sync.py`, and `app/main.py` into your existing repository.
2. In Render → your service → **Environment**, set `AGER_FEED_URL` to:
   `https://dropt.in.ua/index.php?route=export/prom&markup=85&ids=uniq`
   Do not include the `#ufeed...` fragment; URL fragments are not sent to the server.
3. Keep `SYNC_DRY_RUN=true` and `SYNC_LIMIT=10`. Do not enable live writes until the first ten products, prices, images, and stock values have been checked. Shopify products are created as **DRAFT**.
4. Save changes and deploy. Open the service root URL (not `/docs`), enter `APP_API_KEY` from Render, then use **Завантажити свіжий фід**, **Оновити статистику**, **Переглянути товари**, and **Запустити dry-run**.

### Validation performed

Tested locally against `prom_export_85_v2.xml`: parser read all 7,417 offers as 7,417 products; 2,655 had positive stock and 4,762 had zero stock; Python compile check passed. This validates the uploaded XML, not live access to the DROPT URL or live Shopify writes.

### Important

The Render free web service may sleep; the in-process scheduler is not a reliable unattended six-hour sync on a sleeping service. Keep dry-run enabled during validation. Never commit Shopify client secrets or `APP_API_KEY` to GitHub.

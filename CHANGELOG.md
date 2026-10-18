# Changelog

## 1.0.0

### Fixed
- Published products were reset to `DRAFT` (and renamed) on every sync; status/handle are now set on creation only.
- Product handle differed between the lookup (`feed-…`) and the create input (`ager-…`), and SKUs with spaces,
  capitals or Cyrillic never matched on the next run → duplicates. Handles are now deterministic and safe.
- A malformed price crashed `/stats`, `/preview` and `/sync` (`InvalidOperation`); zero/invalid prices got only the
  fixed markup and were created as cheap products. They are now skipped.
- `inf` stock value and empty/broken XML files raised unhandled exceptions.
- The scheduler synced only the first `SYNC_LIMIT` products on every run. `SYNC_LIMIT=0` now means "whole feed";
  `/sync` gained `offset`.
- The last feed could not be deleted (the default feed was re-seeded on every request).
- `X-App-Key` with non-ASCII characters returned HTTP 500.
- Shopify throttling (`429`, `THROTTLED`) and 5xx errors aborted products; requests are now retried with back-off.
  Invalid credentials abort the run instead of failing every product.
- Images were re-uploaded on every sync; descriptions lost paragraphs and were put into `descriptionHtml` unescaped.
- Truncated downloads passed validation; concurrent refreshes shared one temp file.
- Scheduler errors were swallowed silently; they are now logged.
- Dashboard showed `[object Object]` for validation errors; no confirmation on delete; double clicks.

### Changed
- Handles are looked up in one GraphQL request instead of up to three.
- `on_event("startup")` replaced with `lifespan`; code split into `store.py` / `feedfile.py`; dashboard moved to `dashboard.html`.
- Removed the unused `AGER_XML_PATH` and the hard-coded default feed URL.

### Added
- `SYNC_UPDATE_CONTENT`, `FEED_DB_PATH`/`FEED_CACHE_DIR` via `.env`, `LOG_LEVEL`, `offset` for `/sync`, location id accepted as a number.
- Test-suite (`tests/`), `CHANGELOG.md`, `.gitignore`, `.dockerignore`.

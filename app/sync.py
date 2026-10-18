"""Product → Shopify `productSet` synchronisation."""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass
from html import escape
from itertools import islice
from typing import Any, Iterable
from urllib.parse import urlparse

from .parser import Product
from .shopify import ShopifyAuthError, ShopifyClient

log = logging.getLogger(__name__)

HANDLE_PREFIX = "feed-"
LEGACY_HANDLE_PREFIXES = ("ager-",)  # handles created by earlier versions of this importer
SIZE_OPTION_NAMES = {"size", "розмір", "размер"}
OPTION_NAME = "Розмір"
MAX_IMAGES = 10
MAX_TITLE_LEN = 255
_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}


@dataclass
class SyncResult:
    sku: str
    action: str  # create | update | skipped
    product_id: str | None = None
    variants: int = 0
    message: str = ""


# --------------------------------------------------------------------------- helpers
def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def make_handle(sku: str, prefix: str = HANDLE_PREFIX) -> str:
    """Deterministic, Shopify-safe handle. Clean SKUs map 1:1; others get a short hash to avoid collisions."""
    slug = _slug(sku)
    if slug and slug == sku:
        handle = f"{prefix}{slug}"
    else:
        digest = hashlib.sha1(sku.encode("utf-8")).hexdigest()[:8]
        handle = f"{prefix}{slug[:200]}-{digest}" if slug else f"{prefix}{digest}"
    return handle[:255]


def candidate_handles(sku: str) -> list[str]:
    """Handles to look up, newest scheme first. Earlier versions used `<prefix><sku>` without a hash suffix."""
    handles = [make_handle(sku)]
    slug = _slug(sku)
    for prefix in (HANDLE_PREFIX, *LEGACY_HANDLE_PREFIXES):
        handles.append(make_handle(sku, prefix))
        if slug:
            handles.append(f"{prefix}{slug}"[:255])
    return list(dict.fromkeys(handles))


def description_to_html(text: str) -> str:
    """Plain text (as produced by parser.clean_html) → safe HTML paragraphs."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    return "".join(f"<p>{escape(p)}</p>" for p in paragraphs)


def image_filename(sku: str, index: int, url: str) -> str:
    path = urlparse(url).path
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ext = ext if ext in _IMAGE_EXTENSIONS else "jpg"
    base = _slug(sku) or "image"
    return f"{base[:80]}-{index}.{ext}"


def _existing_sizes(existing: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    by_size: dict[str, dict[str, Any]] = {}
    if existing:
        for v in (existing.get("variants") or {}).get("nodes", []):
            size = next((o["value"] for o in v.get("selectedOptions", []) if o["name"].strip().lower() in SIZE_OPTION_NAMES), None)
            if size:
                by_size[size] = v
    return by_size


# --------------------------------------------------------------------------- input builder
def build_input(product: Product, location_id: str, existing: dict[str, Any] | None = None,
                update_content: bool = True) -> dict[str, Any]:
    """Build the `ProductSetInput`.

    * New products are created as DRAFT; existing products keep their status and handle
      (a merchant may have published or renamed them).
    * Images are uploaded only when the product has no media yet, so every sync does not re-upload them.
    """
    is_new = existing is None
    by_size = _existing_sizes(existing)

    variants = []
    for v in product.variants:
        item: dict[str, Any] = {
            "optionValues": [{"optionName": OPTION_NAME, "name": v.size}],
            "price": str(v.price),
            "sku": v.sku,
            "inventoryPolicy": "DENY",
            "inventoryItem": {"tracked": True, "requiresShipping": True, "sku": v.sku},
            "inventoryQuantities": [{"locationId": location_id, "name": "available", "quantity": max(0, int(v.stock))}],
        }
        old = by_size.get(v.size)
        if old:
            item["id"] = old["id"]
        variants.append(item)

    data: dict[str, Any] = {
        "productOptions": [{
            "name": OPTION_NAME,
            "position": 1,
            "values": [{"name": v.size} for v in product.variants],
        }],
        "variants": variants,
    }

    if is_new:
        data["status"] = "DRAFT"
        data["handle"] = make_handle(product.source_sku)

    if is_new or update_content:
        data["title"] = product.title[:MAX_TITLE_LEN]
        data["descriptionHtml"] = description_to_html(product.description)
        data["vendor"] = product.vendor or "DROPT"

        has_media = bool(((existing or {}).get("mediaCount") or {}).get("count"))
        if product.images and not has_media:
            data["files"] = [
                {
                    "originalSource": url,
                    "alt": product.title[:MAX_TITLE_LEN],
                    "filename": image_filename(product.source_sku, i, url),
                    "contentType": "IMAGE",
                }
                for i, url in enumerate(product.images[:MAX_IMAGES], 1)
            ]
    return data


# --------------------------------------------------------------------------- sync
def _sellable(product: Product) -> Product:
    """Drop variants without a positive price; they would be created as free items."""
    good = [v for v in product.variants if v.price > 0]
    if len(good) == len(product.variants):
        return product
    return Product(product.source_sku, product.vendor_code, product.title, product.description,
                   product.vendor, product.url, product.images, good)


def find_existing(client: ShopifyClient, sku: str) -> dict[str, Any] | None:
    existing = client.find_by_handles(candidate_handles(sku))
    if existing is None:
        # Migrate a manually-created product instead of creating a duplicate (exact/prefix SKU match only).
        existing = client.find_by_source_sku(sku)
    return existing


def sync_product(client: ShopifyClient, product: Product, location_id: str, dry_run: bool = True,
                 update_content: bool = True) -> SyncResult:
    sellable = _sellable(product)
    if not sellable.variants:
        return SyncResult(product.source_sku, "skipped", None, 0, "no variant with a positive price")

    existing = find_existing(client, product.source_sku)
    input_data = build_input(sellable, location_id, existing, update_content)
    action = "update" if existing else "create"
    if dry_run:
        return SyncResult(product.source_sku, action, existing.get("id") if existing else None, len(sellable.variants))

    identifier = {"id": existing["id"]} if existing else {"handle": input_data["handle"]}
    payload = client.product_set(input_data, identifier)
    pid = (payload.get("product") or {}).get("id")
    return SyncResult(product.source_sku, action, pid, len(sellable.variants))


def sync_products(client: ShopifyClient, products: Iterable[Product], location_id: str, dry_run: bool = True,
                  limit: int = 0, offset: int = 0, update_content: bool = True) -> list[dict[str, Any]]:
    """Sync `limit` products (0 = all) starting after `offset`. One bad product never aborts the run,
    but rejected credentials do (every following call would fail the same way)."""
    results: list[dict[str, Any]] = []
    for product in islice(products, max(0, offset), None):
        if limit and len(results) >= limit:
            break
        try:
            results.append(asdict(sync_product(client, product, location_id, dry_run, update_content)))
        except ShopifyAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - report per product, keep going
            log.warning("Sync failed for %s: %s", product.source_sku, exc)
            results.append({"sku": product.source_sku, "action": "error", "error": str(exc)[:500]})
    return results

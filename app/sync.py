from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from .parser import Product
from .shopify import ShopifyClient

@dataclass
class SyncResult:
    sku: str
    action: str
    product_id: str | None = None
    variants: int = 0


def build_input(product: Product, location_id: str, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_by_size = {}
    if existing:
        for v in existing.get("variants", {}).get("nodes", []):
            size = next((o["value"] for o in v.get("selectedOptions", []) if o["name"].strip().lower() in {"size", "розмір", "размер"}), None)
            if size:
                existing_by_size[size] = v

    variants = []
    for v in product.variants:
        item: dict[str, Any] = {
            "optionValues": [{"optionName": "Розмір", "name": v.size}],
            "price": str(v.price),
            "sku": v.sku,
            "inventoryPolicy": "DENY",
            "inventoryItem": {"tracked": True, "requiresShipping": True, "sku": v.sku},
            "inventoryQuantities": [{"locationId": location_id, "name": "available", "quantity": v.stock}],
        }
        old = existing_by_size.get(v.size)
        if old:
            item["id"] = old["id"]
        variants.append(item)

    images = []
    for i, url in enumerate(product.images[:10], 1):
        images.append({
            "originalSource": url,
            "alt": product.title,
            "filename": f"{product.source_sku}-{i}.jpg",
            "contentType": "IMAGE",
        })

    return {
        "title": product.title,
        "descriptionHtml": product.description,
        "vendor": product.vendor or "DROPT",
        "handle": f"ager-{product.source_sku}",
        "status": "DRAFT",
        "productOptions": [{
            "name": "Розмір",
            "position": 1,
            "values": [{"name": v.size} for v in product.variants],
        }],
        "variants": variants,
        **({"files": images} if images else {}),
    }


def sync_product(client: ShopifyClient, product: Product, location_id: str, dry_run: bool = True) -> SyncResult:
    handle = f"feed-{product.source_sku}"
    existing = client.find_by_handle(handle)
    if existing is None:
        existing = client.find_by_handle(f"ager-{product.source_sku}")
    if existing is None:
        # Migrate one of the manually-created test products instead of creating a duplicate.
        existing = client.find_by_source_sku(product.source_sku)

    input_data = build_input(product, location_id, existing)
    identifier = {"id": existing["id"]} if existing else {"handle": handle}
    if dry_run:
        return SyncResult(product.source_sku, "update" if existing else "create", existing.get("id") if existing else None, len(product.variants))
    payload = client.product_set(input_data, identifier)
    p = payload.get("product") or {}
    return SyncResult(product.source_sku, "update" if existing else "create", p.get("id"), len(product.variants))

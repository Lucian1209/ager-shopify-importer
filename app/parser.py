from __future__ import annotations
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from html import unescape
import re
from typing import Iterator
from lxml import etree

SIZE_NAMES = {"розмір", "размер", "size"}


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(value)
    value = re.sub(r"<script\b[^>]*>.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def money(value: str | None, percent: Decimal, fixed: Decimal) -> Decimal:
    base = Decimal((value or "0").replace(",", "."))
    result = base * (Decimal("1") + percent / Decimal("100")) + fixed
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Variant:
    size: str
    sku: str
    price: Decimal
    stock: int


@dataclass
class Product:
    source_sku: str
    vendor_code: str
    title: str
    description: str
    vendor: str
    url: str
    images: list[str]
    variants: list[Variant]

    def to_dict(self):
        d = asdict(self)
        d["variants"] = [asdict(v) | {"price": str(v.price)} for v in self.variants]
        d["price"] = str(self.variants[0].price) if self.variants else "0.00"
        d["available"] = bool(self.variants and any(v.stock > 0 for v in self.variants))
        return d


def _text(el, path, default=""):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else default


def _param(el, wanted: set[str]) -> str:
    for p in el.findall("param"):
        if (p.get("name") or "").strip().lower() in wanted:
            return (p.text or "").strip()
    return ""


def _stock(offer) -> int:
    raw = _text(offer, "quantity_in_stock", "0")
    try:
        qty = max(0, int(float(raw)))
    except (ValueError, TypeError):
        qty = 0
    # Prom/YML availability is authoritative: never offer an unavailable item for sale.
    if (offer.get("available") or "").strip().lower() in {"false", "0", "no"}:
        return 0
    return qty


def _product_from_offer(offer, percent: Decimal, fixed: Decimal, prom: bool) -> Product | None:
    # AGER exports group variants by <sku>; Prom/YML exports commonly identify each offer by id.
    sku = (_text(offer, "sku") or offer.get("group_id") or "").strip()
    if prom:
        sku = (offer.get("id") or _text(offer, "model") or sku).strip()
    if not sku:
        return None
    title = _text(offer, "name_ua") or _text(offer, "name")
    if not title:
        return None
    vendor_code = _text(offer, "vendorCode") or _text(offer, "model") or sku
    vendor = _text(offer, "vendor") or ("DROPT" if prom else "AGER")
    desc = clean_html(_text(offer, "description_ua") or _text(offer, "description"))
    images = list(dict.fromkeys((x.text or "").strip() for x in offer.findall("picture") if x.text and (x.text or "").strip()))
    size = _param(offer, SIZE_NAMES) or "Єдиний"
    stock = _stock(offer)
    price = money(_text(offer, "price", "0"), percent, fixed)
    variant_sku = f"{sku}-{size}" if size != "Єдиний" else sku
    variant = Variant(size, variant_sku, price, stock)
    return Product(sku, vendor_code, title, desc, vendor, _text(offer, "url"), images, [variant])


def iter_products(xml_path: str, markup_percent: Decimal = Decimal("0"), markup_fixed: Decimal = Decimal("0")) -> Iterator[Product]:
    # Detect YML/Prom vs AGER by root structure without loading a large XML file into memory.
    root_name = ""
    for event, el in etree.iterparse(xml_path, events=("start",), recover=True, huge_tree=True):
        root_name = etree.QName(el).localname.lower()
        break
    prom = root_name == "yml_catalog"
    if prom:
        for _, offer in etree.iterparse(xml_path, events=("end",), tag="offer", recover=True, huge_tree=True):
            product = _product_from_offer(offer, markup_percent, markup_fixed, True)
            if product is not None:
                yield product
            offer.clear()
        return

    current: Product | None = None
    current_sku: str | None = None
    for _, offer in etree.iterparse(xml_path, events=("end",), tag="offer", recover=True, huge_tree=True):
        sku = (_text(offer, "sku") or offer.get("group_id") or "").strip()
        product = _product_from_offer(offer, markup_percent, markup_fixed, False)
        if product is None or not sku:
            offer.clear(); continue
        if current_sku != sku:
            if current is not None:
                yield current
            current_sku = sku
            current = Product(product.source_sku, product.vendor_code, product.title, product.description, product.vendor, product.url, [], [])
        if current:
            if not current.vendor_code and product.vendor_code: current.vendor_code = product.vendor_code
            if not current.title and product.title: current.title = product.title
            if not current.description and product.description: current.description = product.description
            if not current.url and product.url: current.url = product.url
            if not current.vendor and product.vendor: current.vendor = product.vendor
            for img in product.images:
                if img not in current.images: current.images.append(img)
            current.variants.extend(product.variants)
        offer.clear()
    if current is not None:
        yield current


def merge_duplicate_variants(product: Product) -> Product:
    merged: dict[str, Variant] = {}
    for v in product.variants:
        if v.size not in merged:
            merged[v.size] = v
        else:
            old = merged[v.size]
            merged[v.size] = Variant(v.size, v.sku, v.price, old.stock + v.stock)
    product.variants = list(merged.values())
    return product


def iter_products_grouped(xml_path: str, markup_percent=Decimal("0"), markup_fixed=Decimal("0")) -> Iterator[Product]:
    for p in iter_products(xml_path, markup_percent, markup_fixed):
        yield merge_duplicate_variants(p)

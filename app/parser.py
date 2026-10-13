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
    value = re.sub(r"\s+", " ", value).strip()
    return value

def money(value: str | None, percent: Decimal, fixed: Decimal) -> Decimal:
    base = Decimal(value or "0")
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
        return d

def _text(el, path, default=""):
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else default

def _param(el, wanted: set[str]) -> str:
    for p in el.findall("param"):
        name = (p.get("name") or "").strip().lower()
        if name in wanted:
            return (p.text or "").strip()
    return ""

def iter_products(xml_path: str, markup_percent: Decimal = Decimal("0"), markup_fixed: Decimal = Decimal("0")) -> Iterator[Product]:
    current: Product | None = None
    current_sku = None
    for _, offer in etree.iterparse(xml_path, events=("end",), tag="offer", recover=True, huge_tree=True):
        sku = _text(offer, "sku") or (offer.get("group_id") or "")
        if not sku:
            offer.clear(); continue
        title = _text(offer, "name_ua") or _text(offer, "name")
        vendor_code = _text(offer, "vendorCode")
        size = _param(offer, SIZE_NAMES) or "Єдиний"
        stock_raw = _text(offer, "quantity_in_stock", "0")
        try: stock = max(0, int(float(stock_raw)))
        except ValueError: stock = 0
        price = money(_text(offer, "price", "0"), markup_percent, markup_fixed)
        images = [(x.text or "").strip() for x in offer.findall("picture") if x.text]
        if current_sku != sku:
            if current is not None:
                yield current
            current_sku = sku
            current = Product(sku, vendor_code, title, clean_html(_text(offer, "description_ua") or _text(offer, "description")), "AGER", _text(offer, "url"), [], [])
        if current:
            if not current.vendor_code and vendor_code: current.vendor_code = vendor_code
            if not current.title and title: current.title = title
            if not current.description: current.description = clean_html(_text(offer, "description_ua") or _text(offer, "description"))
            for img in images:
                if img not in current.images: current.images.append(img)
            variant_sku = f"{sku}-{size}" if size != "Єдиний" else sku
            current.variants.append(Variant(size, variant_sku, price, stock))
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
    # Feed is ordered by SKU in the supplied AGЕR export; if the supplier changes order,
    # replace this with a SQLite-backed grouping stage.
    for p in iter_products(xml_path, markup_percent, markup_fixed):
        yield merge_duplicate_variants(p)

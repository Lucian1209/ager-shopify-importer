"""Streaming parser for AGER (grouped offers) and Prom/YML (one offer = one product) feeds."""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import unescape
from typing import Iterator

from lxml import etree

log = logging.getLogger(__name__)

SIZE_NAMES = {"розмір", "размер", "size"}
DEFAULT_SIZE = "Єдиний"
_UNAVAILABLE = {"false", "0", "no"}
_BLOCK_END = re.compile(r"</(?:p|div|li|ul|ol|h[1-6]|tr|table)>|<br\s*/?>", re.I)


# --------------------------------------------------------------------------- helpers
def clean_html(value: str | None) -> str:
    """Convert (possibly escaped) HTML into plain text, keeping paragraph breaks as newlines."""
    if not value:
        return ""
    value = unescape(value)
    value = re.sub(r"<script\b[^>]*>.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", "", value, flags=re.I | re.S)
    value = _BLOCK_END.sub("\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\u00a0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in value.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_decimal(value: str | None) -> Decimal | None:
    """Parse '1 200,50' / '1,200.50' / '99.9'. Returns None for empty or malformed input."""
    if value is None:
        return None
    s = re.sub(r"[\s\u00a0]", "", str(value))
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def money(value: str | None, percent: Decimal, fixed: Decimal) -> Decimal:
    """Apply markup to a feed price. Missing/invalid/non-positive prices stay 0.00 so they can be skipped."""
    base = parse_decimal(value)
    if base is None or base <= 0:
        return Decimal("0.00")
    result = base * (Decimal("1") + percent / Decimal("100")) + fixed
    if result < 0:
        result = Decimal("0")
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


def _rich_text(el, *paths: str) -> str:
    """Text of the first non-empty child, including text nested in child tags (unescaped HTML in a feed)."""
    for path in paths:
        x = el.find(path)
        if x is not None:
            text = "".join(x.itertext()).strip()
            if text:
                return text
    return ""


def _param(el, wanted: set[str]) -> str:
    for p in el.findall("param"):
        if (p.get("name") or "").strip().lower() in wanted:
            return (p.text or "").strip()
    return ""


def _stock(offer) -> int:
    # Prom/YML availability is authoritative: never offer an unavailable item for sale.
    if (offer.get("available") or "").strip().lower() in _UNAVAILABLE:
        return 0
    raw = _text(offer, "quantity_in_stock", "0")
    try:
        return max(0, int(float(raw)))
    except (ValueError, TypeError, OverflowError):
        return 0


def _release(el) -> None:
    """Free an already-processed element AND its processed siblings so memory stays flat on huge feeds."""
    el.clear()
    parent = el.getparent()
    if parent is not None:
        while el.getprevious() is not None:
            del parent[0]


def _product_from_offer(offer, percent: Decimal, fixed: Decimal, prom: bool) -> Product | None:
    # AGER exports group variants by <sku>; Prom/YML exports identify each offer by its id.
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
    desc = clean_html(_rich_text(offer, "description_ua", "description"))
    images = list(dict.fromkeys(
        (x.text or "").strip() for x in offer.findall("picture") if (x.text or "").strip()
    ))
    size = _param(offer, SIZE_NAMES) or DEFAULT_SIZE
    price = money(_text(offer, "price", "0"), percent, fixed)
    variant_sku = f"{sku}-{size}" if size != DEFAULT_SIZE else sku
    variant = Variant(size, variant_sku, price, _stock(offer))
    return Product(sku, vendor_code, title, desc, vendor, _text(offer, "url"), images, [variant])


def _root_name(xml_path: str) -> str:
    try:
        with open(xml_path, "rb") as fh:  # explicit handle: returning early must not leak the file
            for _, el in etree.iterparse(fh, events=("start",), recover=True, huge_tree=True):
                return etree.QName(el).localname.lower()
    except (etree.XMLSyntaxError, OSError):
        pass
    return ""


def _iter_offers(xml_path: str):
    """Yield <offer> elements; a broken/empty file ends the stream instead of crashing the caller."""
    try:
        # Own the file handle: callers (preview/sync with a limit) abandon the generator early,
        # and `with` closes the file deterministically when that happens.
        with open(xml_path, "rb") as fh:
            for _, offer in etree.iterparse(fh, events=("end",), tag="offer", recover=True, huge_tree=True):
                yield offer
    except etree.XMLSyntaxError as exc:
        log.warning("Feed %s is not well-formed XML (%s); using the offers read so far", xml_path, exc)


# --------------------------------------------------------------------------- public API
def iter_products(xml_path: str, markup_percent: Decimal = Decimal("0"), markup_fixed: Decimal = Decimal("0")) -> Iterator[Product]:
    prom = _root_name(xml_path) == "yml_catalog"

    if prom:
        seen: set[str] = set()
        for offer in _iter_offers(xml_path):
            product = _product_from_offer(offer, markup_percent, markup_fixed, True)
            _release(offer)
            if product is None:
                continue
            if product.source_sku in seen:  # duplicate ids would overwrite each other in Shopify
                log.warning("Duplicate offer id %r in feed skipped", product.source_sku)
                continue
            seen.add(product.source_sku)
            yield product
        return

    # AGER: consecutive offers with the same <sku>/group_id are variants of one product.
    current: Product | None = None
    finished: set[str] = set()
    for offer in _iter_offers(xml_path):
        product = _product_from_offer(offer, markup_percent, markup_fixed, False)
        _release(offer)
        if product is None:
            continue
        sku = product.source_sku
        if current is None or current.source_sku != sku:
            if current is not None:
                finished.add(current.source_sku)
                yield current
            if sku in finished:
                log.warning("Offer group %r is not contiguous in the feed; later fragment skipped", sku)
                current = None
                continue
            current = Product(sku, product.vendor_code, product.title, product.description,
                              product.vendor, product.url, [], [])
        for field in ("vendor_code", "title", "description", "url", "vendor"):
            if not getattr(current, field) and getattr(product, field):
                setattr(current, field, getattr(product, field))
        for img in product.images:
            if img not in current.images:
                current.images.append(img)
        current.variants.extend(product.variants)
    if current is not None:
        yield current


def merge_duplicate_variants(product: Product) -> Product:
    """Shopify rejects duplicate option values: merge same-size variants (first SKU/price wins, stock is summed)."""
    merged: dict[str, Variant] = {}
    for v in product.variants:
        if v.size not in merged:
            merged[v.size] = v
        else:
            old = merged[v.size]
            merged[v.size] = Variant(old.size, old.sku, old.price, old.stock + v.stock)
    product.variants = list(merged.values())
    return product


def iter_products_grouped(xml_path: str, markup_percent=Decimal("0"), markup_fixed=Decimal("0")) -> Iterator[Product]:
    for p in iter_products(xml_path, markup_percent, markup_fixed):
        yield merge_duplicate_variants(p)

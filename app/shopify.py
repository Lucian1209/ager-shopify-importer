"""Thin Shopify Admin GraphQL client (client-credentials auth, retries, throttle handling)."""
from __future__ import annotations

import logging
import os
import random
import re
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5

_PRODUCT_FIELDS = """
    id
    handle
    title
    status
    mediaCount { count }
    variants(first: 100) {
      nodes { id sku title selectedOptions { name value } inventoryItem { id tracked } }
    }
"""

PRODUCTS_BY_SKU = (
    "query ProductsBySku($query: String!) {\n"
    "  products(first: 10, query: $query) { nodes {" + _PRODUCT_FIELDS + "} }\n"
    "}"
)

LOCATIONS = """query Locations {
  locations(first: 50, includeInactive: false) { nodes { id name } }
}"""

PRODUCT_SET = """mutation ProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers, $synchronous: Boolean!) {
  productSet(input: $input, identifier: $identifier, synchronous: $synchronous) {
    product { id handle title variants(first: 100) { nodes { id sku price selectedOptions { name value } inventoryItem { id tracked } } } }
    productSetOperation { id status }
    userErrors { field message code }
  }
}"""


class ShopifyError(RuntimeError):
    pass


class ShopifyAuthError(ShopifyError):
    """Credentials were rejected; retrying other products is pointless."""


def normalize_location_id(value: str) -> str:
    """Accept a numeric id ('123456') or a full gid and return the gid form Shopify expects."""
    value = (value or "").strip()
    if value.isdigit():
        return f"gid://shopify/Location/{value}"
    return value


def _quote_search_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ShopifyClient:
    def __init__(self, shop: str | None = None, client_id: str | None = None, client_secret: str | None = None, api_version: str | None = None):
        shop = (shop or os.getenv("SHOPIFY_SHOP", "")).strip()
        self.shop = re.sub(r"^https?://", "", shop, flags=re.I).strip("/")
        self.client_id = (client_id or os.getenv("SHOPIFY_CLIENT_ID", "")).strip()
        self.client_secret = (client_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "")).strip()
        self.api_version = (api_version or os.getenv("SHOPIFY_API_VERSION", "2026-10")).strip()
        if not self.shop or not self.client_id or not self.client_secret:
            raise ShopifyError("SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are required")
        self.url = f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"
        self._token = ""
        self._token_expires_at = 0.0
        self._http: httpx.Client | None = None
        self._sleep = time.sleep  # overridable in tests

    # ------------------------------------------------------------------ transport
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(90, connect=15))
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 120:
            return self._token
        token_url = f"https://{self.shop}/admin/oauth/access_token"
        try:
            response = self._client().post(token_url, timeout=30, data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            })
        except httpx.HTTPError as exc:
            raise ShopifyError(f"Could not obtain Shopify access token: {exc}") from exc
        if response.status_code in (400, 401, 403):
            raise ShopifyAuthError(f"Shopify rejected the credentials ({response.status_code}): {response.text[:300]}")
        if response.is_error:
            raise ShopifyError(f"Shopify token request failed ({response.status_code}): {response.text[:300]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ShopifyError("Shopify token response was not JSON") from exc
        token = body.get("access_token")
        if not token:
            # Do not echo the body: it may contain sensitive details.
            raise ShopifyError("Shopify token response did not contain access_token")
        self._token = token
        self._token_expires_at = time.time() + int(body.get("expires_in", 86399))
        return self._token

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = 0.0
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 0.0
        if delay <= 0:
            delay = min(30.0, 2.0 ** attempt) + random.uniform(0, 0.5)
        log.warning("Shopify throttled/unavailable; retrying in %.1fs", delay)
        self._sleep(min(delay, 60.0))

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        token_refreshed = False
        last_error = "unknown error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": self._access_token()}
            try:
                r = self._client().post(self.url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"request failed: {exc}"
                self._backoff(attempt)
                continue

            if r.status_code in (401, 403):
                if token_refreshed:
                    raise ShopifyAuthError(f"Shopify API rejected the access token ({r.status_code})")
                token_refreshed = True
                self._token = ""
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            if r.is_error:
                raise ShopifyError(f"Shopify API error {r.status_code}: {r.text[:500]}")
            try:
                body = r.json()
            except ValueError as exc:
                raise ShopifyError("Shopify API returned a non-JSON response") from exc

            errors = body.get("errors")
            if errors:
                if isinstance(errors, list) and any(
                    isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors
                ):
                    last_error = "throttled"
                    self._backoff(attempt)
                    continue
                raise ShopifyError(str(errors)[:1000])
            return body.get("data") or {}
        raise ShopifyError(f"Shopify API unavailable after {MAX_ATTEMPTS} attempts: {last_error}")

    # ------------------------------------------------------------------ queries
    def find_by_handles(self, handles: list[str]) -> dict[str, Any] | None:
        """Look up several candidate handles in ONE request; returns the first product found (in order)."""
        handles = list(dict.fromkeys(h for h in handles if h))
        if not handles:
            return None
        defs = ", ".join(f"$h{i}: String!" for i in range(len(handles)))
        body = "\n".join(
            f"  p{i}: productByIdentifier(identifier: {{handle: $h{i}}}) {{{_PRODUCT_FIELDS}}}"
            for i in range(len(handles))
        )
        data = self.gql(f"query ProductsByHandle({defs}) {{\n{body}\n}}", {f"h{i}": h for i, h in enumerate(handles)})
        for i in range(len(handles)):
            if data.get(f"p{i}"):
                return data[f"p{i}"]
        return None

    def find_by_handle(self, handle: str) -> dict[str, Any] | None:
        return self.find_by_handles([handle])

    def find_by_source_sku(self, sku: str) -> dict[str, Any] | None:
        data = self.gql(PRODUCTS_BY_SKU, {"query": f"sku:{_quote_search_value(sku)}"})
        nodes = (data.get("products") or {}).get("nodes") or []
        for product in nodes:
            variants = (product.get("variants") or {}).get("nodes") or []
            if any((v.get("sku") or "") == sku or (v.get("sku") or "").startswith(sku + "-") for v in variants):
                return product
        # Never update a loosely matching product: an unrelated SKU search result is unsafe.
        return None

    def locations(self) -> list[dict[str, str]]:
        return (self.gql(LOCATIONS).get("locations") or {}).get("nodes") or []

    def product_set(self, input_data: dict[str, Any], identifier: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.gql(PRODUCT_SET, {"input": input_data, "identifier": identifier, "synchronous": True})
        payload = data.get("productSet") or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise ShopifyError(str(errors)[:1000])
        return payload

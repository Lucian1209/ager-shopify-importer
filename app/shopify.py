from __future__ import annotations
import os
import time
from typing import Any
import httpx

GRAPHQL = r'''query ProductByHandle($identifier: ProductIdentifierInput!) {
  product: productByIdentifier(identifier: $identifier) {
    id
    handle
    title
    variants(first: 100) {
      nodes { id sku title selectedOptions { name value } inventoryItem { id tracked } }
    }
  }
}'''

PRODUCTS_BY_SKU = r'''query ProductsBySku($query: String!) {
  products(first: 10, query: $query) {
    nodes { id handle title variants(first: 100) { nodes { id sku title selectedOptions { name value } inventoryItem { id tracked } } } }
  }
}'''

LOCATIONS = r'''query Locations {
  locations(first: 50, includeInactive: false) { nodes { id name } }
}'''

PRODUCT_SET = r'''mutation ProductSet($input: ProductSetInput!, $identifier: ProductSetIdentifiers, $synchronous: Boolean!) {
  productSet(input: $input, identifier: $identifier, synchronous: $synchronous) {
    product { id handle title variants(first: 100) { nodes { id sku price selectedOptions { name value } inventoryItem { id tracked } } } }
    productSetOperation { id status }
    userErrors { field message }
  }
}'''

class ShopifyError(RuntimeError):
    pass

class ShopifyClient:
    def __init__(self, shop: str | None = None, client_id: str | None = None, client_secret: str | None = None, api_version: str | None = None):
        self.shop = (shop or os.getenv("SHOPIFY_SHOP", "")).strip().replace("https://", "").replace("http://", "").strip("/")
        self.client_id = client_id or os.getenv("SHOPIFY_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("SHOPIFY_CLIENT_SECRET", "")
        self.api_version = api_version or os.getenv("SHOPIFY_API_VERSION", "2026-10")
        if not self.shop or not self.client_id or not self.client_secret:
            raise ShopifyError("SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are required")
        self.url = f"https://{self.shop}/admin/api/{self.api_version}/graphql.json"
        self._token = ""
        self._token_expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 120:
            return self._token
        token_url = f"https://{self.shop}/admin/oauth/access_token"
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(token_url, data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                })
            if response.is_error:
                raise ShopifyError(f"Shopify token request failed ({response.status_code}): {response.text[:500]}")
            body = response.json()
        except httpx.HTTPError as exc:
            raise ShopifyError(f"Could not obtain Shopify access token: {exc}") from exc
        token = body.get("access_token")
        if not token:
            raise ShopifyError(f"Shopify token response did not contain access_token: {body}")
        self._token = token
        self._token_expires_at = time.time() + int(body.get("expires_in", 86399))
        return self._token

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": self._access_token()}
        try:
            with httpx.Client(timeout=90) as client:
                r = client.post(self.url, json={"query": query, "variables": variables or {}}, headers=headers)
            if r.status_code in (401, 403):
                self._token = ""
                headers["X-Shopify-Access-Token"] = self._access_token()
                with httpx.Client(timeout=90) as client:
                    r = client.post(self.url, json={"query": query, "variables": variables or {}}, headers=headers)
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as exc:
            raise ShopifyError(f"Shopify API request failed: {exc}") from exc
        if body.get("errors"):
            raise ShopifyError(str(body["errors"]))
        return body.get("data", {})

    def find_by_handle(self, handle: str) -> dict[str, Any] | None:
        data = self.gql(GRAPHQL, {"identifier": {"handle": handle}})
        return data.get("product")

    def find_by_source_sku(self, sku: str) -> dict[str, Any] | None:
        data = self.gql(PRODUCTS_BY_SKU, {"query": f"sku:{sku}"})
        nodes = data.get("products", {}).get("nodes", [])
        for product in nodes:
            variants = product.get("variants", {}).get("nodes", [])
            if any((v.get("sku") or "") == sku or (v.get("sku") or "").startswith(sku + "-") for v in variants):
                return product
        # Never update a loosely matching product: an unrelated SKU search result is unsafe.
        return None

    def locations(self) -> list[dict[str, str]]:
        return self.gql(LOCATIONS).get("locations", {}).get("nodes", [])

    def product_set(self, input_data: dict[str, Any], identifier: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self.gql(PRODUCT_SET, {"input": input_data, "identifier": identifier, "synchronous": True})
        payload = data.get("productSet") or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise ShopifyError(str(errors))
        return payload

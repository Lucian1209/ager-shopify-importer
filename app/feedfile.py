"""Download and validate supplier XML feeds."""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
from lxml import etree

MIN_FEED_BYTES = 200
MAX_FEED_BYTES = int(os.getenv("FEED_MAX_MB", "300")) * 1024 * 1024
USER_AGENT = "Mozilla/5.0 FeedImporter/1.0"


class FeedError(RuntimeError):
    pass


def count_offers(path: Path | str) -> int:
    """Number of <offer> elements the (tolerant) parser can read; streaming, constant memory."""
    count = 0
    try:
        for _, el in etree.iterparse(str(path), events=("end",), tag="offer", recover=True, huge_tree=True):
            count += 1
            el.clear()
            parent = el.getparent()
            if parent is not None:
                while el.getprevious() is not None:
                    del parent[0]
    except (etree.XMLSyntaxError, OSError) as exc:
        raise FeedError(f"Feed is not readable XML: {exc}") from exc
    return count


def download_feed(url: str, dest: Path) -> int:
    """Download `url` to `dest` atomically and return the size in bytes.

    The previous cached file is replaced only after the new one passed validation, so a failed or
    truncated download never destroys a good cache. Each call uses its own temp file, so concurrent
    refreshes cannot corrupt each other.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
    try:
        with httpx.stream("GET", url, timeout=httpx.Timeout(180, connect=30), follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as response:
            response.raise_for_status()
            written = 0
            with tmp.open("wb") as out:
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    written += len(chunk)
                    if written > MAX_FEED_BYTES:
                        raise FeedError(f"Feed is larger than {MAX_FEED_BYTES // (1024 * 1024)} MB")
                    out.write(chunk)
            expected = response.headers.get("Content-Length")
            if expected and expected.isdigit() and not response.headers.get("Content-Encoding"):
                if response.num_bytes_downloaded < int(expected):
                    raise FeedError("Download was cut short (fewer bytes than Content-Length)")
        size = tmp.stat().st_size
        if size < MIN_FEED_BYTES:
            raise FeedError("Downloaded feed is unexpectedly small")
        if count_offers(tmp) == 0:
            raise FeedError("Feed contains no <offer> elements")
        tmp.replace(dest)
        return size
    finally:
        tmp.unlink(missing_ok=True)

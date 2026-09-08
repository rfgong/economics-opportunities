#!/usr/bin/env python3
"""
Lightweight page-health/change monitor for economics-opportunities.

This script deliberately does NOT try to semantically parse conference deadlines.
Its job is to:
  - fetch every tracked official page;
  - record whether the source is reachable;
  - hash normalized visible text to detect changes;
  - preserve prior good state on failures;
  - flag repeated source failures for manual review.

The weekly ChatGPT automation is responsible for live semantic verification,
novelty search, and personalized ranking.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VENUES_PATH = ROOT / "venue_calendar.json"
RECRUITING_PATH = ROOT / "recruiting_calendar.json"
STATE_PATH = ROOT / "state.json"

USER_AGENT = (
    "economics-opportunities-cache/1.0 "
    "(public research-opportunity monitor; contact via repository)"
)
TIMEOUT_SECONDS = 20
MAX_BYTES = 3_000_000
RETRIES = 2
MIN_VISIBLE_TEXT_CHARS = 150
MIN_SUCCESS_INTERVAL = timedelta(hours=4)


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalized_visible_text(raw: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    text = raw.decode(charset, errors="replace")

    if "html" in (content_type or "").lower() or "<html" in text[:1000].lower():
        parser = VisibleTextParser()
        parser.feed(text)
        text = " ".join(parser.parts)

    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_page(url: str) -> Tuple[int, str, str]:
    last_error: Exception | None = None

    for attempt in range(RETRIES + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                },
            )
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(MAX_BYTES + 1)

            if len(raw) > MAX_BYTES:
                raw = raw[:MAX_BYTES]

            visible = normalized_visible_text(raw, content_type)
            if status < 200 or status >= 400:
                raise RuntimeError(f"HTTP {status}")
            if len(visible) < MIN_VISIBLE_TEXT_CHARS:
                raise RuntimeError(
                    f"Only {len(visible)} visible characters; refusing to treat as healthy source"
                )

            digest = hashlib.sha256(visible.encode("utf-8")).hexdigest()
            return status, digest, visible[:500]

        except (HTTPError, URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(str(last_error) if last_error else "unknown fetch failure")


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def should_skip_recent_success(state: dict, force: bool) -> bool:
    if force:
        return False
    meta = state.get("_meta", {})
    if meta.get("last_run_ok") is not True:
        return False
    refreshed = parse_iso(meta.get("refreshed_at"))
    if not refreshed:
        return False
    return now_utc() - refreshed < MIN_SUCCESS_INTERVAL


def update_one(
    previous: dict,
    url: str,
    checked_at: datetime,
) -> Tuple[dict, bool]:
    record = dict(previous)
    try:
        status, digest, excerpt = fetch_page(url)
        old_digest = record.get("content_hash")
        changed = bool(old_digest and old_digest != digest)

        record.update(
            {
                "url": url,
                "last_http_status": status,
                "last_checked_at": iso(checked_at),
                "last_success_at": iso(checked_at),
                "content_hash": digest,
                "content_excerpt": excerpt,
                "consecutive_failures": 0,
                "last_error": None,
                "manual_review_required": False,
            }
        )
        if not old_digest:
            record["first_success_at"] = record.get("first_success_at") or iso(checked_at)
            record["last_changed_at"] = record.get("last_changed_at") or iso(checked_at)
        elif changed:
            record["last_changed_at"] = iso(checked_at)

        return record, True

    except Exception as exc:
        failures = int(record.get("consecutive_failures", 0)) + 1
        record.update(
            {
                "url": url,
                "last_checked_at": iso(checked_at),
                "consecutive_failures": failures,
                "last_error": str(exc)[:500],
                "manual_review_required": failures >= 2,
            }
        )
        # Intentionally preserve the prior successful hash, excerpt, success time,
        # and change time. A failed fetch must not erase known-good evidence.
        return record, False


def refresh_group(
    items: Iterable[Tuple[str, dict]],
    prior_group: Dict[str, dict],
    urls_key: str,
) -> Tuple[Dict[str, dict], int]:
    output: Dict[str, dict] = {}
    failures = 0
    checked_at = now_utc()

    for item_id, item in items:
        urls = item.get(urls_key)
        if isinstance(urls, str):
            urls = [urls]
        urls = list(urls or [])

        prior_item = prior_group.get(item_id, {})
        per_url_prior = prior_item.get("sources", {})
        per_url_out: Dict[str, dict] = {}
        item_ok = True

        for url in urls:
            updated, ok = update_one(per_url_prior.get(url, {}), url, checked_at)
            per_url_out[url] = updated
            item_ok = item_ok and ok
            if not ok:
                failures += 1

        output[item_id] = {
            "sources": per_url_out,
            "all_sources_ok": item_ok if urls else False,
            "manual_review_required": any(
                rec.get("manual_review_required") for rec in per_url_out.values()
            ) if urls else True,
        }

    return output, failures


def main() -> int:
    venues = load_json(VENUES_PATH)
    recruiting = load_json(RECRUITING_PATH)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {
        "_meta": {"version": 1},
        "venues": {},
        "recruiting": {},
    }

    force = os.environ.get("FORCE_REFRESH", "").lower() in {"1", "true", "yes"}
    if should_skip_recent_success(state, force):
        print("Recent fully successful refresh found; skipping duplicate scheduled run.")
        return 0

    venue_items = list(venues.get("venues", {}).items())

    # Priority 0 recruiting (Amazon) is always checked first.
    recruiting_items = sorted(
        recruiting.get("organizations", {}).items(),
        key=lambda kv: (int(kv[1].get("priority", 999)), kv[0]),
    )

    venue_state, venue_failures = refresh_group(
        venue_items, state.get("venues", {}), "official_url"
    )
    recruiting_state, recruiting_failures = refresh_group(
        recruiting_items, state.get("recruiting", {}), "official_urls"
    )

    failures = venue_failures + recruiting_failures
    refreshed = now_utc()

    new_state = {
        "_meta": {
            "version": 1,
            "refreshed_at": iso(refreshed),
            "last_run_ok": failures == 0,
            "failure_count": failures,
            "note": (
                "Page hashes are monitoring signals, not proof of an open or closed call. "
                "Weekly semantic verification is performed by the ChatGPT automation."
            ),
        },
        "venues": venue_state,
        "recruiting": recruiting_state,
    }

    atomic_write_json(STATE_PATH, new_state)

    print(
        f"Refreshed {len(venue_items)} venues and {len(recruiting_items)} recruiting "
        f"organizations; source failures={failures}."
    )

    # A partial source failure should be visible in state.json but should not prevent
    # healthy-source updates from being committed. Structural/script failures still exit nonzero.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"Structural refresh failure: {exc}", file=sys.stderr)
        raise SystemExit(1)

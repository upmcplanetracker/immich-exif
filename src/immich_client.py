# Immich API client with multi-key rotation, request timeouts, and
# selective retry (retries only on connection errors, 5xx and 429).
import logging
import random
import threading
import time
from typing import Any, Dict, Generator, List, Optional

import requests


logger = logging.getLogger(__name__)


class ImmichError(Exception):
    """Base class for Immich client errors."""


class ImmichAuthError(ImmichError):
    """All API keys were rejected by the server."""


class ImmichServerError(ImmichError):
    """Server-side error after exhausting retries."""


class ImmichResponseError(ImmichError):
    """Response was not parseable as expected."""


class ApiKeyPool:
    """Thread-safe rotating pool of API keys.

    - rotate(permanent=True) marks a key as dead (401/403) and moves on.
    - rotate(permanent=False) just advances the pointer (429 etc.).
    """

    def __init__(self, keys: List[str]):
        if not keys:
            raise ValueError("ApiKeyPool requires at least one key")
        self._keys = list(keys)
        self._dead: set[str] = set()
        self._idx = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    def alive_count(self) -> int:
        with self._lock:
            return len(self._keys) - len(self._dead)

    def current(self) -> str:
        with self._lock:
            alive = [k for k in self._keys if k not in self._dead]
            if not alive:
                raise ImmichAuthError("All configured Immich API keys were rejected")
            return alive[self._idx % len(alive)]

    def rotate(self, bad_key: str, *, permanent: bool) -> None:
        with self._lock:
            if permanent:
                self._dead.add(bad_key)
                logger.warning(
                    "API key ...%s marked dead (%d/%d keys remaining)",
                    bad_key[-4:], len(self._keys) - len(self._dead), len(self._keys),
                )
            self._idx += 1


class ImmichApiClient:
    """Immich API client. Reuses one requests.Session across calls."""

    DEFAULT_PAGE_SIZE = 1000

    def __init__(self, api_url: str, api_keys, max_retries: int = 3):
        self.api_url = api_url.rstrip('/')
        if isinstance(api_keys, str):
            api_keys = [api_keys]
        self._pool = ApiKeyPool(list(api_keys))
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })

    def close(self) -> None:
        self.session.close()

    # ----- low-level request -----
    def _sleep(self, attempt: int, msg: str, override: Optional[float] = None) -> None:
        wait = min(override if override is not None else 2 ** attempt, 30)
        wait += random.uniform(0, 0.5)  # jitter
        logger.warning("%s; sleeping %.1fs", msg, wait)
        time.sleep(wait)

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Any:
        url = f"{self.api_url}{endpoint}"
        kwargs.setdefault('timeout', (10, 60))
        keys_tried = 0

        while True:
            key = self._pool.current()
            self.session.headers['X-API-Key'] = key
            last_status: Optional[int] = None

            for attempt in range(self.max_retries + 1):
                try:
                    r = self.session.request(method, url, **kwargs)
                except requests.exceptions.RequestException as e:
                    if attempt == self.max_retries:
                        raise ImmichServerError(
                            f"{method} {endpoint} failed after "
                            f"{self.max_retries + 1} attempts: {e}") from e
                    self._sleep(attempt, f"{method} {endpoint}: {e}")
                    continue

                last_status = r.status_code

                if last_status in (401, 403):
                    break  # rotate key, don't retry this one

                if last_status == 429:
                    retry_after = r.headers.get('Retry-After')
                    override = float(retry_after) if retry_after else None
                    if attempt == self.max_retries:
                        break  # rotate
                    self._sleep(attempt, f"429 on {endpoint}", override=override)
                    continue

                if 500 <= last_status < 600:
                    if attempt == self.max_retries:
                        raise ImmichServerError(
                            f"{method} {endpoint} -> {last_status}")
                    self._sleep(attempt, f"{method} {endpoint} -> {last_status}")
                    continue

                r.raise_for_status()
                if not r.content:
                    return {}
                try:
                    return r.json()
                except ValueError as e:
                    raise ImmichResponseError(
                        f"Non-JSON response from {endpoint}: {e}") from e

            # key exhausted -> rotate
            keys_tried += 1
            self._pool.rotate(key, permanent=last_status in (401, 403))
            # Use alive_count() so permanently-dead keys don't inflate the
            # budget and cause extra pointless round-trips.
            if keys_tried > self._pool.alive_count() + 1:
                raise ImmichAuthError(
                    f"Exhausted key rotation for {method} {endpoint} "
                    f"(last status {last_status})")

    # ----- API methods -----
    def search_assets_updated_since(self, timestamp: str,
                                    page: Optional[int] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'updatedAfter': timestamp}
        if page is not None:
            payload['page'] = page
        return self._make_request('POST', '/api/search/metadata', json=payload)

    def get_all_assets_paginated(
        self, updated_after: Optional[str] = None,
    ) -> Generator[List[Dict], None, None]:
        """Yield asset batches. Uses Immich's `nextPage` field when present."""
        timestamp = updated_after or "1800-01-01T00:00:00.000Z"
        page: int = 1

        while True:
            response = self.search_assets_updated_since(timestamp, page)
            assets_block = response.get('assets') or {}
            items = assets_block.get('items') or []

            if not items:
                break

            logger.debug("Retrieved %d assets on page %s", len(items), page)
            yield items

            next_page = assets_block.get('nextPage')
            if next_page is not None and next_page != '':
                try:
                    page = int(next_page)
                except (TypeError, ValueError):
                    page += 1
            else:
                # Older API without nextPage: stop when a short page is returned.
                if len(items) < self.DEFAULT_PAGE_SIZE:
                    break
                page += 1

    def get_asset_by_id(self, asset_id: str) -> Dict[str, Any]:
        return self._make_request('GET', f'/api/assets/{asset_id}')

    def get_all_albums(self) -> List[Dict[str, Any]]:
        """Return all albums. Raises on failure (never returns a silent [])."""
        response = self._make_request('GET', '/api/albums')
        albums = response if isinstance(response, list) else []
        logger.debug("Retrieved %d albums", len(albums))
        return albums

    def get_album_by_id(self, album_id: str) -> Dict[str, Any]:
        return self._make_request('GET', f'/api/albums/{album_id}')

    def build_asset_album_map(self) -> Dict[str, List[str]]:
        """Map asset_id -> list of album names.

        Raises if the initial album list cannot be fetched (so callers can
        abort the sync instead of silently emptying everyone's keywords).
        Per-album fetch failures are logged and skipped.
        """
        asset_album_map: Dict[str, List[str]] = {}
        albums = self.get_all_albums()  # raises on failure

        for album in albums:
            album_name = album.get('albumName') or ''
            album_id = album.get('id') or ''
            if not album_name or not album_id:
                logger.warning("Skipping album with missing name/id: %r", album)
                continue
            try:
                album_details = self.get_album_by_id(album_id)
            except Exception as e:
                logger.warning(
                    "Could not fetch album %r (%s): %s — its keywords will be "
                    "left untouched", album_name, album_id, e)
                continue
            for asset in album_details.get('assets') or []:
                aid = asset.get('id')
                if aid:
                    asset_album_map.setdefault(aid, []).append(album_name)

        logger.info(
            "Asset-album map: %d assets across %d albums",
            len(asset_album_map), len(albums))
        return asset_album_map

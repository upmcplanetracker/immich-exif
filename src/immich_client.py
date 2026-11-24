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
        """Yield asset batches from Immich metadata search with pagination.

        Current API returns assets as a list and nextPage at response root;
        older nested response shapes are also supported.
        """
        timestamp = updated_after or "1800-01-01T00:00:00.000Z"
        page: int = 1
        seen_pages = set()


        while True:
            if page in seen_pages:
                raise ImmichResponseError(
                    f"Immich metadata-search pagination loop detected at page {page}")
            seen_pages.add(page)

            response = self.search_assets_updated_since(timestamp, page)
            if not isinstance(response, dict):
                raise ImmichResponseError(
                    f"Unexpected metadata-search response type: {type(response).__name__}")


            assets_value = response.get('assets')
            if isinstance(assets_value, list):
                # Current Immich API: assets array; nextPage at response root.
                items = assets_value
                next_page = response.get('nextPage')
            elif isinstance(assets_value, dict):
                # Legacy response shape: assets.items and assets.nextPage.
                items = assets_value.get('items') or []
                next_page = assets_value.get('nextPage')
            elif assets_value is None:
                raise ImmichResponseError(
                    "Metadata-search response is missing its 'assets' field")
            else:
                raise ImmichResponseError(
                    f"Unexpected metadata-search assets type: "
                    f"{type(assets_value).__name__}")

            if not isinstance(items, list):
                raise ImmichResponseError(
                    f"Metadata-search assets are not a list (page {page})")
            if not items:
                break


            logger.debug("Retrieved %d assets on page %s", len(items), page)
            yield items



            if next_page is not None and next_page != '':
                try:
                    page = int(next_page)
                except (TypeError, ValueError):
                    page += 1
            else:
                # Older API without nextPage: stop on a short page.
                if len(items) < self.DEFAULT_PAGE_SIZE:
                    break
                page += 1


    def get_asset_by_id(self, asset_id: str) -> Dict[str, Any]:
        return self._make_request('GET', f'/api/assets/{asset_id}')

    def get_all_albums(self) -> List[Dict[str, Any]]:
        """Return all albums. Raises on failure (never returns a silent [])."""
        response = self._make_request('GET', '/api/albums')
        if not isinstance(response, list):
            raise ImmichResponseError(
                f"Unexpected album-list response type: {type(response).__name__}"
            )
        logger.debug("Retrieved %d albums", len(response))
        return response

    def get_album_by_id(self, album_id: str) -> Dict[str, Any]:
        return self._make_request('GET', f'/api/albums/{album_id}')

    def get_album_assets_paginated(
        self, album_id: str,
    ) -> Generator[List[Dict[str, Any]], None, None]:
        """Yield an album's assets using Immich's album-scoped metadata search.

        Immich v3 removed `assets` from AlbumResponseDto. The search response
        may use either assets.items/assets.nextPage or a flat assets list with
        nextPage at the response root, so both shapes are supported.
        """
        page = 1
        seen_pages = set()
        while True:
            if page in seen_pages:
                raise ImmichResponseError(
                    f"Album pagination loop for {album_id} at page {page}")
            seen_pages.add(page)
            response = self._make_request(
                'POST', '/api/search/metadata',
                json={'albumIds': [album_id], 'page': page,
                      'size': self.DEFAULT_PAGE_SIZE},
            )
            if not isinstance(response, dict):
                raise ImmichResponseError(
                    f"Unexpected album-search response type: {type(response).__name__}")
            value = response.get('assets')
            if isinstance(value, list):
                items, next_page = value, response.get('nextPage')
            elif isinstance(value, dict):
                items, next_page = value.get('items') or [], value.get('nextPage')
            elif value is None:
                raise ImmichResponseError(
                    f"Album search response for {album_id} is missing 'assets'")
            else:
                raise ImmichResponseError(
                    f"Unexpected album-search assets type: {type(value).__name__}")
            if not isinstance(items, list):
                raise ImmichResponseError(
                    f"Album search assets are not a list for {album_id}, page {page}")
            if not items:
                break
            logger.debug("Retrieved %d assets for album %s page %s",
                         len(items), album_id, page)
            yield items
            if next_page is not None and next_page != '':
                try:
                    next_number = int(next_page)
                except (TypeError, ValueError) as e:
                    raise ImmichResponseError(
                        f"Invalid nextPage {next_page!r} for album {album_id}") from e
                if next_number <= page:
                    raise ImmichResponseError(
                        f"Non-advancing nextPage {next_number} for album {album_id}")
                page = next_number
            elif len(items) >= self.DEFAULT_PAGE_SIZE:
                page += 1
            else:
                break

    def build_asset_album_map(self) -> Dict[str, List[str]]:
        """Map asset IDs to album names using album-scoped metadata search."""
        asset_album_map: Dict[str, List[str]] = {}
        albums = self.get_all_albums()  # raises on failure
        total = len(albums)
        for index, album in enumerate(albums, start=1):
            album_name = album.get('albumName') or ''
            album_id = album.get('id') or ''
            if not album_name or not album_id:
                raise ImmichResponseError(
                    f"Album list contains an entry without a name or ID: {album!r}"
                )
            try:
                for batch in self.get_album_assets_paginated(album_id):
                    for asset in batch:
                        aid = asset.get('id')
                        if aid:
                            names = asset_album_map.setdefault(aid, [])
                            if album_name not in names:
                                names.append(album_name)
            except Exception as e:
                # A partial album map is unsafe: the sync engine could mistake
                # an inaccessible album for a real membership removal and
                # delete its managed keywords. Abort the whole sync instead.
                logger.error(
                    "Could not fetch assets for album %r (%s); aborting album "
                    "scan to avoid applying incomplete keyword data: %s",
                    album_name, album_id, e,
                )
                raise ImmichResponseError(
                    f"Could not build complete asset-album map for album "
                    f"{album_name!r} ({album_id})"
                ) from e
            if index % 100 == 0 or index == total:
                logger.info("Album scan progress: %d/%d albums; %d assets mapped",
                            index, total, len(asset_album_map))
        logger.info("Asset-album map: %d assets across %d albums",
                    len(asset_album_map), total)
        return asset_album_map

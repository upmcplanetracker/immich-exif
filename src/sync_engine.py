# Sync engine: fetches changed assets from Immich and writes EXIF to files.
import logging
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .exif_writer import ExifWriter
from .immich_client import ImmichApiClient
from .state_manager import StateManager


logger = logging.getLogger(__name__)


class ProcessResult(Enum):
    OK = "ok"
    SKIPPED = "skipped"
    ERROR = "error"


class SyncEngine:
    def __init__(self, client: ImmichApiClient, exif_writer: ExifWriter,
                 state_manager: StateManager, media_path: str):
        self.client = client
        self.exif_writer = exif_writer
        self.state_manager = state_manager
        self.media_path = media_path.rstrip('/')

    def run_sync(self, stop_event: Optional[threading.Event] = None) -> Tuple[int, int]:
        """Run a sync cycle. Returns (processed, errors)."""
        # Capture start time BEFORE work — otherwise edits made during the
        # sync can be silently lost on the next run.
        sync_started_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.") + "000Z"

        last_sync = self.state_manager.get_last_sync_time()
        if last_sync is None:
            last_sync = "1800-01-01T00:00:00.000Z"
            logger.info("First sync — fetching all assets")
        else:
            logger.info("Incremental sync since %s", last_sync)

        try:
            asset_album_map = self.client.build_asset_album_map()
        except Exception as e:
            logger.error("Cannot build asset-album map, aborting sync: %s", e)
            return 0, 1

        # An empty map is valid — the user may not use albums at all.
        # build_asset_album_map() already raises on API failure, so reaching
        # here with an empty dict just means zero album memberships.
        if not asset_album_map:
            logger.debug("Asset-album map is empty (no albums configured)")

        try:
            assets_to_process = self._collect_candidates(last_sync,
                                                        asset_album_map)
        except Exception as e:
            logger.error("Asset enumeration failed: %s", e)
            return 0, 1

        # Prune state entries for assets that no longer exist in the library.
        # We know the full universe: everything the paginated search returned
        # union everything in the album map (assets in albums but not updated).
        known_ids = set(assets_to_process) | set(asset_album_map.keys())
        self.state_manager.prune_assets(known_ids)

        logger.info("Processing %d assets", len(assets_to_process))

        processed = 0
        errors = 0
        for i, asset_id in enumerate(assets_to_process, start=1):
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested; finishing after %d/%d assets",
                            i - 1, len(assets_to_process))
                break

            try:
                result = self._process_asset(asset_id, asset_album_map)
            except Exception as e:
                logger.exception("Unhandled error for asset %s: %s",
                                 asset_id, e)
                result = ProcessResult.ERROR

            if result is ProcessResult.OK:
                processed += 1
            elif result is ProcessResult.ERROR:
                errors += 1

            if i % 50 == 0:
                try:
                    self.state_manager.save()
                except Exception as e:
                    logger.error("Periodic state save failed: %s", e)
                logger.info("Progress: %d/%d", i, len(assets_to_process))

        self.state_manager.set_last_sync_time(sync_started_at)
        try:
            self.state_manager.save()
        except Exception as e:
            logger.error("Final state save failed: %s", e)

        logger.info("Sync done: %d processed, %d errors", processed, errors)
        return processed, errors

    def _collect_candidates(self, last_sync: str,
                           asset_album_map: Dict[str, List[str]]) -> List[str]:
        ids: set[str] = set()
        for batch in self.client.get_all_assets_paginated(last_sync):
            for a in batch:
                aid = a.get('id')
                if aid:
                    ids.add(aid)

        # Include assets whose album membership may have changed.
        for asset_id, current_albums in asset_album_map.items():
            prev = self.state_manager.get_asset_metadata(asset_id)
            if prev is None:
                ids.add(asset_id)
                continue
            prev_albums = prev.get('albums') or []
            if sorted(prev_albums) != sorted(current_albums):
                ids.add(asset_id)
        return sorted(ids)

    def _process_asset(self, asset_id: str,
                      asset_album_map: Dict[str, List[str]]) -> ProcessResult:
        try:
            asset = self.client.get_asset_by_id(asset_id)
        except Exception as e:
            logger.error("Fetch asset %s failed: %s", asset_id, e)
            return ProcessResult.ERROR

        metadata = self._map_asset_to_metadata(asset, asset_album_map)

        if not self.state_manager.has_metadata_changed(asset_id, metadata):
            return ProcessResult.SKIPPED

        file_path = self._get_file_path(metadata.get('original_path', ''))
        if not file_path or not os.path.exists(file_path):
            logger.warning(
                "File missing for asset %s (originalPath=%r, tried=%r)",
                asset_id, metadata.get('original_path'), file_path)
            return ProcessResult.ERROR

        previous = self.state_manager.get_asset_metadata(asset_id)
        if not self.exif_writer.write_metadata(file_path, metadata, previous):
            logger.error("EXIF write failed for %s", file_path)
            return ProcessResult.ERROR

        self.state_manager.set_asset_metadata(asset_id, metadata)
        return ProcessResult.OK

    def _map_asset_to_metadata(self, asset: Dict[str, Any],
                              asset_album_map: Dict[str, List[str]]) -> Dict[str, Any]:
        asset_id = asset.get('id', '')
        exif = asset.get('exifInfo') or {}
        return {
            'albums': list(asset_album_map.get(asset_id, [])),
            'is_favorite': bool(asset.get('isFavorite', False)),
            'description': exif.get('description') or '',
            'latitude': exif.get('latitude'),
            'longitude': exif.get('longitude'),
            'date_time': exif.get('dateTimeOriginal') or '',
            'original_path': asset.get('originalPath') or '',
        }

    def _get_file_path(self, original_path: str) -> Optional[str]:
        if not original_path:
            return None
        media = os.path.normpath(self.media_path)
        original = os.path.normpath(original_path)

        # Exact match or proper prefix (avoids /mnt/media matching /mnt/media_backup).
        if original == media or original.startswith(media + os.sep):
            return original

        if not original.startswith('/'):
            return os.path.join(media, original)

        # Absolute but outside media_path. Best-effort translation — this is
        # the path most commonly hit when MEDIA_PATH is misconfigured, so the
        # caller logs both originalPath and the resulting path on failure.
        return os.path.join(media, original.lstrip('/'))

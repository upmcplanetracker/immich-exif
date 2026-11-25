"""Fetch changed Immich assets and write their metadata to the media files."""

import logging
import os
import threading
import time
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
    READ_ONLY_VIDEO = "read_only_video"
    ERROR = "error"


class SyncEngine:
    def __init__(
        self,
        client: ImmichApiClient,
        exif_writer: ExifWriter,
        state_manager: StateManager,
        media_path: str,
    ):
        self.client = client
        self.exif_writer = exif_writer
        self.state_manager = state_manager
        # Keep the persistence layer aligned with the writer even when tests
        # or callers pass an explicit dry_run constructor argument.
        self.state_manager.dry_run = self.exif_writer.dry_run
        self.media_path = os.path.abspath(media_path)

    def run_sync(
        self, stop_event: Optional[threading.Event] = None
    ) -> Tuple[int, int]:
        """Run one sync cycle and return (successfully processed, errors).

        The incremental-search watermark advances only after a complete,
        error-free run. Failed or interrupted runs are retried next time.
        Dry-runs never change or save persistent sync state.
        """
        dry_run = self.exif_writer.dry_run
        run_started = time.monotonic()
        sync_started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )

        last_sync = self.state_manager.get_last_sync_time()
        if last_sync is None:
            last_sync = "1800-01-01T00:00:00.000Z"
            logger.info("First sync: fetching all assets")
        else:
            logger.info("Incremental sync since %s", last_sync)

        try:
            asset_album_map = self.client.build_asset_album_map()
        except Exception:
            logger.exception("Cannot build asset-album map; aborting this sync")
            return 0, 1

        if not asset_album_map:
            logger.debug("Asset-album map is empty")

        try:
            assets_to_process = self._collect_candidates(last_sync, asset_album_map)
        except Exception:
            logger.exception("Asset enumeration failed; aborting this sync")
            return 0, 1

        # Never prune from an incremental result. It is not a complete list of
        # library assets, so doing so would discard valid cache entries.
        logger.info(
            "%s: %d candidate assets",
            "DRY RUN" if dry_run else "Processing",
            len(assets_to_process),
        )

        processed = 0
        skipped_unchanged = 0
        skipped_read_only = 0
        errors = 0
        checked = 0
        interrupted = False

        for index, asset_id in enumerate(assets_to_process, start=1):
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                logger.info(
                    "Stop requested; ending after %d of %d assets",
                    index - 1,
                    len(assets_to_process),
                )
                break

            checked += 1
            try:
                result = self._process_asset(asset_id, asset_album_map)
            except Exception:
                logger.exception("Unhandled error while processing asset %s", asset_id)
                result = ProcessResult.ERROR

            if result is ProcessResult.OK:
                processed += 1
            elif result is ProcessResult.SKIPPED:
                skipped_unchanged += 1
            elif result is ProcessResult.READ_ONLY_VIDEO:
                skipped_read_only += 1
            elif result is ProcessResult.ERROR:
                errors += 1

            if index % 50 == 0:
                if not dry_run:
                    try:
                        self.state_manager.save()
                    except Exception:
                        logger.exception("Periodic state save failed")
                logger.info("Progress: %d/%d", index, len(assets_to_process))

        if stop_event is not None and stop_event.is_set():
            interrupted = True

        duration = time.monotonic() - run_started
        logger.info(
            "SYNC RUN SUMMARY%s\n"
            "Assets checked: %d/%d candidates\n"
            "Processed successfully: %d\n"
            "Skipped (unchanged): %d\n"
            "Skipped (read-only video): %d\n"
            "Errors: %d\n"
            "Interrupted: %s\n"
            "Duration: %.1f seconds",
            " (DRY RUN)" if dry_run else "",
            checked,
            len(assets_to_process),
            processed,
            skipped_unchanged,
            skipped_read_only,
            errors,
            "Yes" if interrupted else "No",
            duration,
        )

        if dry_run:
            logger.info("DRY RUN: Persistent state was not changed.")
            return processed, errors

        # Save successful per-asset work even if another asset failed. Do not
        # advance the watermark unless every candidate was handled cleanly;
        # this ensures failed/interrupted assets are included in the next run.
        if not interrupted and errors == 0:
            self.state_manager.set_last_sync_time(sync_started_at)
        else:
            logger.warning(
                "Not advancing last_sync_time (interrupted=%s, errors=%d); "
                "unresolved assets will be retried next run",
                interrupted,
                errors,
            )

        try:
            self.state_manager.save()
        except Exception:
            logger.exception("Final state save failed")

        logger.info("Sync complete: %d processed, %d errors", processed, errors)
        return processed, errors

    def _collect_candidates(
        self, last_sync: str, asset_album_map: Dict[str, List[str]]
    ) -> List[str]:
        """Collect updated assets plus assets whose album membership changed."""
        asset_ids: set[str] = set()

        for batch in self.client.get_all_assets_paginated(last_sync):
            for asset in batch:
                asset_id = asset.get("id")
                if asset_id:
                    asset_ids.add(asset_id)

        # Check both current album members and cached assets. The second group
        # is important: it detects an asset removed from its last album.
        cached_ids = self.state_manager.get_asset_ids()
        for asset_id in cached_ids | set(asset_album_map):
            previous = self.state_manager.get_asset_metadata(asset_id) or {}
            previous_albums = sorted(previous.get("albums") or [])
            current_albums = sorted(asset_album_map.get(asset_id, []))
            if previous_albums != current_albums:
                asset_ids.add(asset_id)

        return sorted(asset_ids)

    def _process_asset(
        self, asset_id: str, asset_album_map: Dict[str, List[str]]
    ) -> ProcessResult:
        try:
            asset = self.client.get_asset_by_id(asset_id)
        except Exception:
            logger.exception("Fetching asset %s failed", asset_id)
            return ProcessResult.ERROR

        metadata = self._map_asset_to_metadata(asset, asset_album_map)
        if not self.state_manager.has_metadata_changed(asset_id, metadata):
            return ProcessResult.SKIPPED

        file_path = self._get_file_path(metadata.get("original_path", ""))
        if not file_path or not os.path.isfile(file_path):
            logger.warning(
                "File missing for asset %s (originalPath=%r, resolved=%r)",
                asset_id,
                metadata.get("original_path"),
                file_path,
            )
            return ProcessResult.ERROR

        # Some containers (MKV/WebM/AVI) are intentionally read-only for this
        # ExifTool build. Mark them handled in state so unchanged assets are not
        # reconsidered on every incremental run, but report them as skipped.
        if self.exif_writer.is_read_only_video(file_path):
            logger.info("Metadata write skipped (read-only video): %s", file_path)
            if not self.exif_writer.dry_run:
                self.state_manager.set_asset_metadata(asset_id, metadata)
            return ProcessResult.READ_ONLY_VIDEO


        previous = self.state_manager.get_asset_metadata(asset_id)
        if not self.exif_writer.write_metadata(file_path, metadata, previous):
            logger.error("EXIF write failed for %s", file_path)
            return ProcessResult.ERROR

        # A dry-run returns success to mean "would write", but must not mark
        # the asset as synchronized. The next real run must still process it.
        if not self.exif_writer.dry_run:
            self.state_manager.set_asset_metadata(asset_id, metadata)

        return ProcessResult.OK

    @staticmethod
    def _map_asset_to_metadata(
        asset: Dict[str, Any], asset_album_map: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        asset_id = asset.get("id", "")
        exif = asset.get("exifInfo") or {}
        return {
            "albums": list(asset_album_map.get(asset_id, [])),
            "is_favorite": bool(asset.get("isFavorite", False)),
            "description": exif.get("description") or "",
            "latitude": exif.get("latitude"),
            "longitude": exif.get("longitude"),
            "date_time": exif.get("dateTimeOriginal") or "",
            "original_path": asset.get("originalPath") or "",
        }

    def _get_file_path(self, original_path: str) -> Optional[str]:
        """Map Immich's API path into MEDIA_PATH without allowing path escape."""
        if not original_path:
            return None

        media_root = os.path.realpath(self.media_path)
        original = str(original_path).replace("\\", "/")
        normalized = os.path.normpath(original)

        prefixes = (
            "/usr/src/app/upload/",
            "upload/",
        )
        relative: Optional[str] = None
        for prefix in prefixes:
            if normalized.startswith(prefix):
                relative = normalized[len(prefix):]
                break

        if relative is None:
            if os.path.isabs(normalized):
                # Already resolved inside the mounted media directory.
                absolute_original = os.path.realpath(normalized)
                try:
                    if os.path.commonpath((media_root, absolute_original)) == media_root:
                        return absolute_original
                except ValueError:
                    pass
                # Some Immich versions return an absolute path from inside
                # the container. Preserve the prior best-effort behavior by
                # interpreting an unknown absolute path relative to MEDIA_PATH.
                relative = normalized.lstrip("/")
            else:
                relative = normalized

        candidate = os.path.realpath(os.path.join(media_root, relative))
        try:
            if os.path.commonpath((media_root, candidate)) != media_root:
                logger.error("Refusing path outside MEDIA_PATH: %r", original_path)
                return None
        except ValueError:
            logger.error("Refusing invalid media path: %r", original_path)
            return None

        return candidate

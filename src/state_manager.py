"""Persistent sync state for the Immich EXIF writer.

State is written atomically so an interrupted process cannot leave a partial
JSON file. Schema v3 deliberately clears the v2 asset cache: earlier dry-runs
could have recorded metadata even though no EXIF changes were written.
"""

import copy
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional, Set


logger = logging.getLogger(__name__)

_STATE_VERSION = 3

_DEFAULT_STATE: Dict[str, Any] = {
    "version": _STATE_VERSION,
    "last_sync_time": None,
    "asset_metadata": {},
    "stats": {
        "total_synced": 0,
        "last_sync_duration": 0.0,
        "errors": 0,
    },
}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


class StateManager:
    """Load, query, and safely persist per-asset sync metadata."""

    def __init__(self, state_file_path: str = "/app/state/sync_state.json"):
        self.state_file_path = state_file_path
        self.dry_run = _env_bool("EXIF_DRY_RUN", False)
        self._state: Dict[str, Any] = copy.deepcopy(_DEFAULT_STATE)
        self.load()

    def load(self) -> None:
        """Load state, resetting older schemas and quarantining corrupt JSON."""
        if not os.path.exists(self.state_file_path):
            logger.info("No existing state file at %s", self.state_file_path)
            return

        try:
            with open(self.state_file_path, "r", encoding="utf-8") as state_file:
                loaded = json.load(state_file)
        except json.JSONDecodeError as exc:
            self._quarantine_corrupt_file(exc)
            return
        except OSError as exc:
            logger.error("Could not read state file %s: %s", self.state_file_path, exc)
            return

        if not isinstance(loaded, dict):
            logger.error("State file is not a JSON object; starting with empty state")
            return

        file_version = loaded.get("version", 1)
        if not isinstance(file_version, int):
            logger.warning("Invalid state schema version %r; starting fresh", file_version)
            return

        if file_version > _STATE_VERSION:
            raise RuntimeError(
                f"State file schema v{file_version} is newer than supported "
                f"v{_STATE_VERSION}; refusing to overwrite it"
            )

        if file_version < _STATE_VERSION:
            old_stats = loaded.get("stats")
            self._state = copy.deepcopy(_DEFAULT_STATE)
            if isinstance(old_stats, dict):
                for key in self._state["stats"]:
                    if key in old_stats:
                        self._state["stats"][key] = old_stats[key]
            logger.warning(
                "State schema v%d -> v%d: clearing cached asset metadata and "
                "last sync time to force a full re-sync. This also repairs "
                "assets that may have been marked synced during an old dry-run.",
                file_version,
                _STATE_VERSION,
            )
            return

        last_sync_time = loaded.get("last_sync_time")
        self._state["last_sync_time"] = (
            last_sync_time if isinstance(last_sync_time, str) else None
        )

        asset_metadata = loaded.get("asset_metadata")
        if isinstance(asset_metadata, dict):
            self._state["asset_metadata"] = asset_metadata
        else:
            logger.warning("State asset_metadata is not an object; starting with an empty cache")

        stats = loaded.get("stats")
        if isinstance(stats, dict):
            for key in self._state["stats"]:
                if key in stats:
                    self._state["stats"][key] = stats[key]

        logger.info("Loaded state: %d assets", len(self._state["asset_metadata"]))

    def _quarantine_corrupt_file(self, error: Exception) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        quarantine_path = f"{self.state_file_path}.corrupt-{stamp}"
        try:
            os.replace(self.state_file_path, quarantine_path)
            logger.error(
                "State file was corrupt (%s); moved it to %s. A full re-sync will follow.",
                error,
                quarantine_path,
            )
        except OSError as rename_error:
            logger.error(
                "State file was corrupt (%s) and could not be quarantined (%s); "
                "starting with empty in-memory state",
                error,
                rename_error,
            )

    def save(self) -> None:
        """Atomically save state. In dry-run mode, never touch the state file."""
        if self.dry_run:
            logger.debug("DRY RUN: state save suppressed")
            return

        self._state["version"] = _STATE_VERSION
        state_dir = os.path.dirname(self.state_file_path) or "."
        os.makedirs(state_dir, exist_ok=True)

        fd, temporary_path = tempfile.mkstemp(
            prefix=".sync-state-", suffix=".tmp", dir=state_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump(self._state, state_file, indent=2, default=str)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.state_file_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    def get_last_sync_time(self) -> Optional[str]:
        return self._state.get("last_sync_time")

    def set_last_sync_time(self, timestamp: str) -> None:
        self._state["last_sync_time"] = timestamp

    def get_asset_metadata(self, asset_id: str) -> Optional[Dict[str, Any]]:
        return self._state["asset_metadata"].get(asset_id)

    def get_asset_ids(self) -> Set[str]:
        """Return IDs currently represented in the cache."""
        return set(self._state["asset_metadata"].keys())

    def set_asset_metadata(self, asset_id: str, metadata: Dict[str, Any]) -> None:
        self._state["asset_metadata"][asset_id] = copy.deepcopy(metadata)

    def remove_asset_metadata(self, asset_id: str) -> None:
        self._state["asset_metadata"].pop(asset_id, None)

    def prune_assets(self, known_ids: Set[str]) -> int:
        """Remove cached IDs absent from a *complete* library inventory.

        Do not call this with the results of an incremental search: those results
        are not a complete inventory. The sync engine intentionally does not
        prune automatically for that reason.
        """
        current_ids = self.get_asset_ids()
        stale_ids = current_ids - known_ids
        for asset_id in stale_ids:
            self.remove_asset_metadata(asset_id)
        if stale_ids:
            logger.info("Pruned %d stale asset entries from state", len(stale_ids))
        return len(stale_ids)

    def has_metadata_changed(
        self, asset_id: str, new_metadata: Dict[str, Any]
    ) -> bool:
        previous = self.get_asset_metadata(asset_id)
        if previous is None:
            return True

        fields = (
            "albums",
            "is_favorite",
            "description",
            "latitude",
            "longitude",
            "date_time",
            "original_path",
        )
        for field in fields:
            old_value = previous.get(field)
            new_value = new_metadata.get(field)
            if field == "albums":
                if sorted(old_value or []) != sorted(new_value or []):
                    return True
            elif old_value != new_value:
                return True
        return False

    def update_stats(
        self,
        total_synced_delta: int = 0,
        duration: float = 0.0,
        errors_delta: int = 0,
    ) -> None:
        stats = self._state["stats"]
        stats["total_synced"] += total_synced_delta
        stats["last_sync_duration"] = duration
        stats["errors"] += errors_delta

    def get_stats(self) -> Dict[str, Any]:
        return copy.deepcopy(self._state["stats"])

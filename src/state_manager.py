# Sync state persisted to a single JSON file (atomic writes).
#
# Schema versions:
#   v1 — original code. Keyword handling was destructive.
#   v2 — current. On upgrade from v1, asset_metadata is cleared and
#        last_sync_time is reset to force one full re-sync. This repairs
#        files whose keywords were previously wiped by the old code.
import copy
import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional, Set


logger = logging.getLogger(__name__)


_STATE_VERSION = 2

_DEFAULT_STATE: Dict[str, Any] = {
    "version": _STATE_VERSION,
    "last_sync_time": None,
    "asset_metadata": {},
    "stats": {"total_synced": 0, "last_sync_duration": 0.0, "errors": 0},
}


class StateManager:
    def __init__(self, state_file_path: str = "/app/state/sync_state.json"):
        self.state_file_path = state_file_path
        self._state = copy.deepcopy(_DEFAULT_STATE)
        self.load()

    # ----- load/save -----
    def load(self) -> None:
        if not os.path.exists(self.state_file_path):
            logger.info("No existing state file at %s", self.state_file_path)
            return
        try:
            with open(self.state_file_path, 'r') as f:
                loaded = json.load(f)
        except json.JSONDecodeError as e:
            self._quarantine_corrupt_file(e)
            return
        except IOError as e:
            logger.error("Could not read state file %s: %s",
                         self.state_file_path, e)
            return

        if not isinstance(loaded, dict):
            logger.error("State file is not a JSON object; ignoring content")
            return

        file_version = loaded.get("version", 1)
        if file_version < _STATE_VERSION:
            logger.warning(
                "State file schema v%d -> v%d. Clearing cached asset metadata "
                "to force a one-time full re-sync. This is expected on upgrade "
                "and will re-add keywords to files that were previously wiped.",
                file_version, _STATE_VERSION)
            old_stats = loaded.get("stats")
            self._state = copy.deepcopy(_DEFAULT_STATE)
            if isinstance(old_stats, dict):
                self._state["stats"].update(old_stats)
            return

        # Normal load path (current version).
        self._state["last_sync_time"] = loaded.get(
            "last_sync_time", self._state["last_sync_time"])
        am = loaded.get("asset_metadata")
        if isinstance(am, dict):
            self._state["asset_metadata"] = am
        stats = loaded.get("stats")
        if isinstance(stats, dict):
            self._state["stats"].update(stats)
        logger.info("Loaded state: %d assets",
                    len(self._state["asset_metadata"]))

    def _quarantine_corrupt_file(self, err: Exception) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        quarantine = f"{self.state_file_path}.corrupt-{stamp}"
        try:
            os.rename(self.state_file_path, quarantine)
            logger.error(
                "State file was corrupt (%s). Renamed to %s; the next sync "
                "will reprocess the entire library.", err, quarantine)
        except OSError as e:
            logger.error(
                "State file was corrupt (%s) and could not be quarantined (%s); "
                "starting fresh.", err, e)

    def save(self) -> None:
        self._state["version"] = _STATE_VERSION
        state_dir = os.path.dirname(self.state_file_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(dir=state_dir or '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self._state, f, indent=2, default=str)
            os.replace(tmp_path, self.state_file_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ----- sync time -----
    def get_last_sync_time(self) -> Optional[str]:
        return self._state.get("last_sync_time")

    def set_last_sync_time(self, timestamp: str) -> None:
        self._state["last_sync_time"] = timestamp

    # ----- per-asset metadata -----
    def get_asset_metadata(self, asset_id: str) -> Optional[Dict[str, Any]]:
        return self._state["asset_metadata"].get(asset_id)

    def set_asset_metadata(self, asset_id: str, metadata: Dict[str, Any]) -> None:
        # deepcopy so callers can't accidentally mutate stored state
        # (albums is a list and would otherwise be shared).
        self._state["asset_metadata"][asset_id] = copy.deepcopy(metadata)

    def remove_asset_metadata(self, asset_id: str) -> None:
        self._state["asset_metadata"].pop(asset_id, None)

    def prune_assets(self, known_ids: Set[str]) -> int:
        """Drop state for assets not present in known_ids. Returns count removed."""
        current = set(self._state["asset_metadata"].keys())
        stale = current - known_ids
        for aid in stale:
            del self._state["asset_metadata"][aid]
        if stale:
            logger.info("Pruned %d stale asset entries from state", len(stale))
        return len(stale)

    def has_metadata_changed(self, asset_id: str,
                            new_metadata: Dict[str, Any]) -> bool:
        prev = self.get_asset_metadata(asset_id)
        if prev is None:
            return True
        for field in ('albums', 'is_favorite', 'description',
                      'latitude', 'longitude', 'date_time', 'original_path'):
            old_v = prev.get(field)
            new_v = new_metadata.get(field)
            if field == 'albums':
                if sorted(old_v or []) != sorted(new_v or []):
                    return True
            elif old_v != new_v:
                return True
        return False

    # ----- stats -----
    def update_stats(self, total_synced_delta: int = 0,
                    duration: float = 0.0, errors_delta: int = 0) -> None:
        s = self._state["stats"]
        s["total_synced"] += total_synced_delta
        s["last_sync_duration"] = duration
        s["errors"] += errors_delta

    def get_stats(self) -> Dict[str, Any]:
        return copy.deepcopy(self._state["stats"])
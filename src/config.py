# Configuration loaded entirely from environment variables (or .env).
# Resolution order:
#   real environment > .env file > built-in defaults
# Required: IMMICH_URL and one of IMMICH_API_KEYS / IMMICH_API_KEY.
# Optional: PATH_MAPPINGS (comma/newline-separated 'immich_prefix=local_prefix' pairs).
import os
import re
from typing import Any, Dict, List, Tuple


def _parse_keys(raw: str) -> List[str]:
    """Split a comma/newline-separated string into a list of keys."""
    return [k.strip() for k in re.split(r'[,\n]', raw or '') if k.strip()]


def _parse_path_mappings(raw: str) -> List[Tuple[str, str]]:
    """Parse 'immich_prefix=local_prefix' pairs separated by commas or newlines."""
    mappings: List[Tuple[str, str]] = []
    for entry in re.split(r'[,\n]', raw or ''):
        entry = entry.strip()
        if not entry:
            continue
        if '=' not in entry:
            raise ValueError(
                f"Invalid PATH_MAPPINGS entry {entry!r}; expected 'immich_prefix=local_prefix'"
            )
        src, dst = entry.split('=', 1)
        src_clean = os.path.normpath(src.strip().replace('\\', '/'))
        dst_clean = dst.strip()
        if not src_clean or src_clean == '.' or not dst_clean:
            raise ValueError(
                f"Invalid PATH_MAPPINGS entry {entry!r}; both source and destination are required"
            )
        mappings.append((src_clean, os.path.abspath(dst_clean)))

    # Match the longest (most specific) prefix first.
    mappings.sort(key=lambda pair: len(pair[0]), reverse=True)
    return mappings


class Config:
    """Env-only configuration with .env file support."""

    def __init__(self, env_file: str = ".env"):
        self._load_dotenv(env_file)
        self._config = self._build_config()
        self._validate_config()

    def _load_dotenv(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            from dotenv import load_dotenv
        except ImportError:
            # Running in a container with env vars already set; dotenv is optional.
            return
        # override=False so real environment always wins over .env
        load_dotenv(path, override=False)

    def _build_config(self) -> Dict[str, Any]:
        return {
            'immich': {
                'url': (os.environ.get('IMMICH_URL') or '').strip(),
                'api_keys': self._resolve_keys(),
            },
            'paths': {
                'path_mappings': _parse_path_mappings(
                    os.environ.get('PATH_MAPPINGS') or ''
                ),
            },
            'sync': {
                'interval': self._int_env('SYNC_INTERVAL', 900),
            },
            'logging': {
                'level': (os.environ.get('LOG_LEVEL') or 'INFO').upper(),
                'file': os.environ.get('LOG_FILE') or '/app/logs/exif-writer.log',
            },
            'state': {
                'file': os.environ.get('STATE_FILE') or '/app/state/sync_state.json',
            },
        }

    def _resolve_keys(self) -> List[str]:
        # IMMICH_API_KEYS takes precedence; IMMICH_API_KEY is a legacy fallback.
        if raw := os.environ.get('IMMICH_API_KEYS'):
            return _parse_keys(raw)
        if raw := os.environ.get('IMMICH_API_KEY'):
            return _parse_keys(raw)
        return []

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None or raw == '':
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer, got {raw!r}") from None

    def _validate_config(self) -> None:
        immich = self._config['immich']
        if not immich['url']:
            raise ValueError("IMMICH_URL is required")
        if not immich['url'].startswith(('http://', 'https://')):
            raise ValueError(
                f"IMMICH_URL must start with http:// or https://, got {immich['url']!r}"
            )
        if not immich['api_keys']:
            raise ValueError(
                "No Immich API key configured. Set IMMICH_API_KEYS=key1,key2 "
                "(or IMMICH_API_KEY=key1)."
            )
        for k in immich['api_keys']:
            if k.startswith('YOUR_') and k.endswith('_HERE'):
                raise ValueError("API key is still a placeholder")
        if self._config['sync']['interval'] <= 0:
            raise ValueError("SYNC_INTERVAL must be positive")
        if self._config['logging']['level'] not in {
            'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
        }:
            raise ValueError(
                f"LOG_LEVEL must be DEBUG/INFO/WARNING/ERROR/CRITICAL, "
                f"got {self._config['logging']['level']!r}"
            )

    # ----- accessors -----
    @property
    def immich_url(self) -> str:
        return self._config['immich']['url'].rstrip('/')

    @property
    def immich_api_keys(self) -> List[str]:
        return list(self._config['immich']['api_keys'])

    @property
    def path_mappings(self) -> List[Tuple[str, str]]:
        return list(self._config['paths']['path_mappings'])

    @property
    def sync_interval(self) -> int:
        return self._config['sync']['interval']

    @property
    def log_level(self) -> str:
        return self._config['logging']['level']

    @property
    def log_file(self) -> str:
        return self._config['logging']['file']

    @property
    def state_file(self) -> str:
        return self._config['state']['file']
```[cite: 4]

---

### `main.py`

```python
# Entry point: run one independent sync cycle for each configured Immich user.
import logging
import os
import signal
import threading
import time
import traceback
from datetime import datetime, timedelta

from .config import Config
from .exif_writer import ExifWriter
from .immich_client import ImmichApiClient
from .state_manager import StateManager
from .sync_engine import SyncEngine


def setup_logging(config: Config) -> logging.Logger:
    log_dir = os.path.dirname(config.log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    level = getattr(logging, config.log_level.upper())
    file_handler = logging.FileHandler(config.log_file)
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(fmt))
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt))

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    root.addHandler(console)
    return logging.getLogger(__name__)


def _state_path_for_account(base_path: str, account_number: int) -> str:
    # Preserve the existing state file for account 1.
    if account_number == 1:
        return base_path
    root, extension = os.path.splitext(base_path)
    return f"{root}.account-{account_number}{extension or '.json'}"


def main():
    logger = None
    exif_writer = None
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        if logger:
            logger.info("Received %s; stopping after the current asset/account", name)
        stop_event.set()

    try:
        config = Config()
        logger = setup_logging(config)
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        api_keys = config.immich_api_keys
        logger.info("=" * 60)
        logger.info("Immich EXIF Writer starting")
        logger.info("URL: %s", config.immich_url)
        if config.path_mappings:
            logger.info(
                "Path mappings: %s",
                ", ".join(f"{src} -> {dst}" for src, dst in config.path_mappings),
            )
        else:
            logger.info("Path mappings: none (using direct Immich paths)")
        logger.info("Sync interval: %ds", config.sync_interval)
        logger.info("Immich accounts configured: %d", len(api_keys))
        logger.info("Base state file: %s", config.state_file)
        logger.info(
            "Dry run: %s   Keep backups: %s",
            os.environ.get('EXIF_DRY_RUN', '0'),
            os.environ.get('EXIF_KEEP_BACKUPS', '1'),
        )
        logger.info("=" * 60)

        exif_writer = ExifWriter()
        run_number = 0

        while not stop_event.is_set():
            run_number += 1
            cycle_start = time.time()
            cycle_processed = 0
            cycle_errors = 0

            logger.info("-" * 60)
            logger.info(
                "Starting sync cycle #%d for %d account(s)",
                run_number,
                len(api_keys),
            )

            # Each key is a separate user identity, not a failover credential.
            for account_number, api_key in enumerate(api_keys, start=1):
                if stop_event.is_set():
                    break

                state_path = _state_path_for_account(
                    config.state_file, account_number
                )
                client = None
                logger.info(
                    "Account %d/%d starting (state=%s, API key suffix=...%s)",
                    account_number,
                    len(api_keys),
                    state_path,
                    api_key[-4:],
                )

                try:
                    # Pass exactly one key to this account's client.
                    client = ImmichApiClient(config.immich_url, [api_key])
                    state_manager = StateManager(state_file_path=state_path)
                    engine = SyncEngine(
                        client,
                        exif_writer,
                        state_manager,
                        path_mappings=config.path_mappings,
                    )

                    account_start = time.time()
                    processed, errors = engine.run_sync(stop_event=stop_event)
                    duration = time.time() - account_start

                    state_manager.update_stats(
                        total_synced_delta=processed,
                        duration=duration,
                        errors_delta=errors,
                    )
                    try:
                        state_manager.save()
                    except Exception as exc:
                        logger.error(
                            "State save failed for account %d: %s",
                            account_number,
                            exc,
                        )
                        errors += 1

                    cycle_processed += processed
                    cycle_errors += errors
                    logger.info(
                        "Account %d/%d complete: %d processed, %d errors, %.1fs",
                        account_number,
                        len(api_keys),
                        processed,
                        errors,
                        duration,
                    )

                except Exception as exc:
                    cycle_errors += 1
                    logger.error(
                        "Account %d/%d failed: %s",
                        account_number,
                        len(api_keys),
                        exc,
                    )
                    logger.error(traceback.format_exc())
                    # A failed account must not prevent the next account syncing.
                finally:
                    if client is not None:
                        try:
                            client.close()
                        except Exception:
                            logger.exception(
                                "Could not close account %d client",
                                account_number,
                            )

            logger.info(
                "SYNC CYCLE #%d COMPLETE: %d processed, %d errors, %.1fs",
                run_number,
                cycle_processed,
                cycle_errors,
                time.time() - cycle_start,
            )

            if stop_event.is_set():
                break
            next_time = datetime.now() + timedelta(seconds=config.sync_interval)
            logger.info(
                "Next full account cycle at %s",
                next_time.strftime('%Y-%m-%d %H:%M:%S'),
            )
            stop_event.wait(config.sync_interval)

    except Exception as exc:
        if logger:
            logger.error("Fatal error: %s", exc)
            logger.error(traceback.format_exc())
        else:
            print(f"Fatal error during startup: {exc}")
            traceback.print_exc()
    finally:
        if exif_writer is not None:
            try:
                exif_writer.close()
            except Exception:
                pass
        if logger:
            logger.info("Shutdown complete")


if __name__ == "__main__":
    main()

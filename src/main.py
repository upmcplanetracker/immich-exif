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
    # File logging is optional. Defaults to enabled to preserve existing behavior.
    log_to_file = os.environ.get("LOG_TO_FILE", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }

    fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    level = getattr(logging, config.log_level.upper())
    formatter = logging.Formatter(fmt)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if log_to_file:
        log_dir = os.path.dirname(config.log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = logging.FileHandler(config.log_file)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    else:
        # Console-only logging: no file or log-directory is created.
        root.addHandler(console)

    # Also retain console output when file logging is enabled.
    if log_to_file:
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

                try:
                    # Pass exactly one key to this account's client.
                    client = ImmichApiClient(config.immich_url, [api_key])

                    # Resolve the Immich display name for this key once up front
                    # so every subsequent log line identifies the account by name
                    # rather than a positional index.
                    try:
                        user_info = client.get_current_user()
                        account_name = (
                            user_info.get('name')
                            or user_info.get('email')
                            or f'account-{account_number}'
                        )
                    except Exception as exc:
                        account_name = f'account-{account_number}'
                        logger.warning(
                            "Could not resolve display name for account %d "
                            "(key suffix ...%s): %s — falling back to '%s'",
                            account_number, api_key[-4:], exc, account_name,
                        )

                    logger.info(
                        "Account %d/%d [%s] starting (state=%s, API key suffix=...%s)",
                        account_number,
                        len(api_keys),
                        account_name,
                        state_path,
                        api_key[-4:],
                    )

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
                            "State save failed for account %d [%s]: %s",
                            account_number,
                            account_name,
                            exc,
                        )
                        errors += 1

                    cycle_processed += processed
                    cycle_errors += errors
                    logger.info(
                        "Account %d/%d [%s] complete: %d processed, %d errors, %.1fs",
                        account_number,
                        len(api_keys),
                        account_name,
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

# Entry point: load config, set up logging/signals, run continuous sync loop.
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
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(console)
    return logging.getLogger(__name__)


def main():
    logger: logging.Logger | None = None
    client: ImmichApiClient | None = None
    exif_writer: ExifWriter | None = None
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        name = signal.Signals(signum).name
        if logger:
            logger.info("Received %s; will stop after current asset", name)
        stop_event.set()

    try:
        config = Config()
        logger = setup_logging(config)

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        logger.info("=" * 60)
        logger.info("Immich EXIF Writer starting")
        logger.info("URL: %s", config.immich_url)
        logger.info("Media path: %s", config.media_path)
        logger.info("Sync interval: %ds", config.sync_interval)
        logger.info("API keys loaded: %d", len(config.immich_api_keys))
        logger.info("State file: %s", config.state_file)
        logger.info("Dry run: %s   Keep backups: %s",
                    os.environ.get('EXIF_DRY_RUN', '0'),
                    os.environ.get('EXIF_KEEP_BACKUPS', '1'))
        logger.info("=" * 60)

        client = ImmichApiClient(config.immich_url, config.immich_api_keys)
        exif_writer = ExifWriter()
        state_manager = StateManager(state_file_path=config.state_file)
        engine = SyncEngine(client, exif_writer, state_manager,
                            config.media_path)

        run_number = 0
        while not stop_event.is_set():
            run_number += 1
            logger.info("-" * 50)
            logger.info("Sync run #%d", run_number)
            start = time.time()
            try:
                processed, errors = engine.run_sync(stop_event=stop_event)
                duration = time.time() - start
                state_manager.update_stats(
                    total_synced_delta=processed,
                    duration=duration,
                    errors_delta=errors)
                try:
                    state_manager.save()
                except Exception as e:
                    logger.error("State save after sync failed: %s", e)
                logger.info(
                    "Run #%d complete: %d processed, %d errors, %.1fs",
                    run_number, processed, errors, duration)
            except Exception as e:
                logger.error("Sync run #%d crashed: %s", run_number, e)
                logger.error(traceback.format_exc())

            if stop_event.is_set():
                break
            next_time = datetime.now() + timedelta(seconds=config.sync_interval)
            logger.info("Next sync at %s",
                        next_time.strftime('%Y-%m-%d %H:%M:%S'))
            stop_event.wait(config.sync_interval)

    except Exception as e:
        if logger:
            logger.error("Fatal error: %s", e)
            logger.error(traceback.format_exc())
        else:
            print(f"Fatal error during startup: {e}")
            traceback.print_exc()

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if exif_writer is not None:
            try:
                exif_writer.close()
            except Exception:
                pass
        if logger:
            logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
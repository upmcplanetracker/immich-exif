This was forked from another repo and I vibe coded the crap out of it to get it working.  Will put in more details later.
immich-exif
===========

Writes metadata from your [Immich](https://immich.app) library (albums, favorite status, description, GPS coordinates, capture date) into the EXIF / XMP tags of the underlying image and video files.

**Why this exists:** Immich stores metadata in its database, not in the files. When you add a photo to an album in Immich, the `.jpg` on disk is unchanged — so external tools (Lightroom, digiKam, PhotoPrism, plain `exiftool`) don't see your albums, ratings, or descriptions. This service bridges that gap by reading from the Immich API and writing to the files via ExifTool.

How it works
------------

Source

What it provides

Immich API

albums, favorite, description, GPS, date

Filesystem (`MEDIA_PATH`)

the actual `.jpg` / `.mp4` bytes to modify

The Immich API is **read-only** for our purposes. There is no Immich endpoint that writes EXIF into files. The container must have read/write access to your Immich library volume at `MEDIA_PATH`.

Features
--------

*   Incremental sync — only touches assets that changed in Immich
*   Multiple API keys with automatic rotation on 401/403/429
*   Per-file diff — only writes tags that actually changed
*   **Non-destructive keyword handling** — only removes keywords it wrote itself (verified against state); user-added keywords are never touched
*   Correct GPS hemisphere handling (`abs` + explicit `N/S/E/W` ref)
*   ISO 8601 → EXIF date conversion (refuses to write garbage)
*   Dry-run and backup modes for safe first runs
*   Atomic state writes; corrupt state is quarantined, not silently discarded
*   Graceful shutdown on SIGTERM (SIGKILL-proof via `tini`)
*   One persistent ExifTool subprocess for the whole sync (fast)

Requirements
------------

*   A running Immich instance and an API key from **User Settings → API Keys**
*   The Immich upload directory bind-mounted into the container (read/write)
*   ExifTool ≥ 12.57 (tested through 13.55)

Quick start
-----------

### 1\. Create `.env`

    cp .env.example .env
    $EDITOR .env

Minimum required:

    IMMICH_URL=http://immich-server:2283
    IMMICH_API_KEYS=your-api-key-here
    MEDIA_PATH=/mnt/media

`MEDIA_PATH` must be **the path inside this container**, not inside the `immich-server` container. The most common misconfiguration is copying `/usr/src/app/upload` from Immich's container — that path doesn't exist here.

### 2\. docker-compose.yml

    services:
      immich-exif:
        build: .
        container_name: immich-exif
        restart: unless-stopped
        env_file: .env
        volumes:
          # Same host path that Immich's UPLOAD_LOCATION points to.
          # Left side = host path; right side = MEDIA_PATH in .env.
          - /path/to/immich/upload:/mnt/media
          - ./state:/app/state
          - ./logs:/app/logs

### 3\. Build and run — safely

Set `EXIF_DRY_RUN=1` in `.env` before the first start. Then run:

    docker compose build
    docker compose up -d
    docker compose logs -f immich-exif

This is a continuously running service, so use `up -d` for the dry run rather
than `docker compose run --rm`. Review the `[DRY RUN]` commands in the logs.
No media files or persistent sync state are changed while dry-run mode is on.

When the log looks right, change `EXIF_DRY_RUN=0` in `.env` and recreate the
service:

    docker compose up -d --force-recreate

The first real run may recheck the full library if the state file was created
by an older version. Keep backups enabled for that run.

Configuration
-------------

All configuration is via environment variables (or a `.env` file next to the process). Real environment variables always override `.env`.

Variable

Required

Default

Description

`IMMICH_URL`

**yes**

—

Immich base URL, e.g. `http://immich-server:2283`. Must start with `http://` or `https://`.

`IMMICH_API_KEYS`

**yes**†

—

One or more API keys, **comma-separated on a single line**. Rotated automatically.

`IMMICH_API_KEY`

**yes**†

—

Single-key fallback. Used only if `IMMICH_API_KEYS` is not set.

`MEDIA_PATH`

**yes**

—

Path to the Immich library **as mounted in this container**. Must exist and be readable/writable.

`SYNC_INTERVAL`

no

`900`

Seconds between sync runs. `900` = 15 min.

`LOG_LEVEL`

no

`INFO`

`DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`.

`LOG_FILE`

no

`/app/logs/exif-writer.log`

Log file path.

`STATE_FILE`

no

`/app/state/sync_state.json`

Where sync state is persisted.

`EXIF_DRY_RUN`

no

`0`

If `1`, log intended writes and modify nothing. The sync-state file is also
left unchanged, so a later real run will still process the assets.

`EXIF_KEEP_BACKUPS`

no

`1`

If `1`, ExifTool saves `<file>_original` before editing. Set `0` only after you trust the tool.

† One of `IMMICH_API_KEYS` or `IMMICH_API_KEY` is required. If both are present, `IMMICH_API_KEYS` wins.

Startup fails fast with a clear `ValueError` if any required variable is missing, if a key is still the placeholder `YOUR_API_KEY_HERE`, if `SYNC_INTERVAL` isn't a positive integer, or if `LOG_LEVEL` isn't a known level.

Multiple API keys
-----------------

The client accepts a comma-separated list of keys and rotates through them:

    IMMICH_API_KEYS=key1,key2,key3

Behavior:

*   **401 / 403** (auth failed) → key marked dead permanently, next key used, logged at `WARNING` with the last 4 chars only.
*   **429** (rate-limited) → next key used immediately; if a `Retry-After` header is present, honors it.
*   **5xx / connection errors** → retries the same key with exponential backoff and jitter (up to 3 attempts). If all attempts fail, next key used.
*   **If all keys are dead** → `ImmichAuthError` raised; sync cycle aborts, process continues running and retries next interval.

Thread-safe. Whitespace around keys is trimmed. Newlines in the env var are also accepted as separators, but prefer single-line comma-separated to keep docker-compose `env_file:` parsing identical.

Safety features
---------------

### Dry run

Set `EXIF_DRY_RUN=1` and every intended ExifTool command is logged at `INFO`
with a `[DRY RUN]` prefix. Media files are not modified, asset metadata is not
committed to the sync cache, the sync timestamp is not advanced, and the state
file is not saved. You can inspect the dry-run output, turn dry-run off, and
run again without losing the work that needs to be done.

### Backups

With the default `EXIF_KEEP_BACKUPS=1`, ExifTool saves a sibling `<original-name>_original` file before each modification. Example:

    /photo/2023/IMG_1234.jpg           <- modified
    /photo/2023/IMG_1234.jpg_original  <- byte-for-byte original

After you've confirmed a sync looks right, you can remove backups:

    find /path/to/immich/upload -name '*_original' -delete

Or disable them for future runs with `EXIF_KEEP_BACKUPS=0` (not recommended until you trust the tool).

### Keyword safety

The old version of this tool removed **all** existing keywords before writing albums, destroying user-added keywords. This version is surgical:

*   Reads current keywords from the file (via ExifTool `get_tags`)
*   Removes only keywords that (a) are present on disk _and_ (b) we recorded writing on the previous run (from `STATE_FILE`)
*   Adds any new albums as keywords
*   Keywords you added manually via Lightroom, digiKam, etc. are **never** removed

### State schema migration

The state file has a `version` field. Schema v3 clears the cached asset metadata
and resets the last-sync timestamp when upgrading from v1 or v2. This is
intentional: older dry-runs could have marked assets as synchronized without
writing their files. The next real run therefore rechecks the full library.
Expect one longer sync after this upgrade.

The sync engine does not prune cached assets from an incremental search because
that search is not a complete inventory of the library. It also does not
advance the sync timestamp when a run is interrupted or any asset fails; those
assets are retried on the next cycle.
If Immich returns an error while reading any album, the album-map scan aborts
the sync instead of continuing with incomplete data. This prevents a temporary
API failure from being mistaken for albums being removed.

Corrupt state files (bad JSON) are renamed to `sync_state.json.corrupt-<timestamp>` and logged at `ERROR`, rather than silently triggering a mystery full resync.

Verifying a file after a run
----------------------------

For a photo:

    exiftool -Keywords -Subject -Rating -Description \
             -GPSLatitude -GPSLatitudeRef \
             -GPSLongitude -GPSLongitudeRef \
             -DateTimeOriginal \
             /mnt/media/library/.../photo.jpg

For a video:

    exiftool -Subject -Rating -Description -CreateDate \
             /mnt/media/library/.../video.mp4

To see exactly what changed vs the backup:

    exiftool -diff photo.jpg photo.jpg_original

Troubleshooting
---------------

### Every asset logs "File missing"

`MEDIA_PATH` is wrong. Two common mistakes:

1.  **Copied from Immich's container** — `/usr/src/app/upload` is Immich's internal path. Your container needs the path where the volume is mounted _here_, usually `/mnt/media` or similar.
2.  **Missing volume mount** — the compose file needs a line like `- /host/path:/mnt/media` matching your `MEDIA_PATH`.

The `WARNING` log line shows both `originalPath` (what Immich reported) and `tried` (the path we computed). Compare them to diagnose.

### `All configured Immich API keys were rejected`

Either the keys are wrong/expired, or they belong to a user without library access. Regenerate in Immich and update `.env`.

### Videos not getting keywords

Videos are tagged with `XMP:Subject`. Some players/DAM tools only read `QuickTime:Keywords` or `Keys:Keywords`. If your tool doesn't show video keywords, try `exiftool -XMP:Subject -a video.mp4` to confirm the tag is present; if it is, the issue is your tool's reader, not the writer.

### Photos suddenly have fewer keywords

Check the diff against the backup. If keywords were removed, they must have matched our previous album list. If they were user-added but happen to match an album name you've since removed, they'd be removed — this is the inherent trade-off of surgical diffing vs. never removing anything. If you want "add only, never remove" behavior, that's a one-line change in `_keyword_params` (remove the `to_remove` block); say so and I'll patch it.

### Logs show `[DRY RUN]` but you didn't set `EXIF_DRY_RUN`

Check that `EXIF_DRY_RUN=1` isn't set in your shell environment, your `docker-compose.yml` `environment:` block, or inherited from the parent process. `printenv | grep EXIF` shows what's in the current shell.

### Container takes a long time to shut down

If using `docker compose stop`, the process should exit within a few seconds because `tini` + SIGTERM handler + `stop_event.wait()` (not `time.sleep()`) means in-flight work is finished and the process exits cleanly. If it takes longer than ~10 seconds, check that you built with the new `Dockerfile` (which has `tini`). Without tini, Python's SIGTERM handler is still called, but child processes (ExifTool) can be orphaned.

ExifTool version note
---------------------

The provided `Dockerfile` builds ExifTool **13.55** from source (overriding Debian bookworm's 12.57) and verifies the installed version during the build. If you need a different version, change the `ARG EXIFTOOL_VERSION` line near the top of the `Dockerfile` and rebuild. To confirm the version in a built image:

    docker run --rm <your-image> exiftool -ver

Running locally (without Docker)
--------------------------------

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # Requires exiftool on PATH: https://exiftool.org/install.html
    
    cp .env.example .env
    $EDITOR .env
    
    EXIF_DRY_RUN=1 python -m src.main

Requires `src/__init__.py` to exist for the package import to work (it should already be in the repo).

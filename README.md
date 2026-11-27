immich-exif
===========
AI Disclosure -
This was forked from another repo that seems like it isn't being developed anymore and had some issues that I wanted to fix (https://github.com/jmathai/immich-exif) and I vibe coded the crap out of it to squash bugs, add .envs and multiple api support, change how it logs errors, etc. etc.

if the dev reappears I will abandon this project if they want and they can pick it up again. unlike me they seem to be a real programmer.  I am not. 

I've run this lots of my setup but consider a backup before running this just in case. 

Writes metadata from your [Immich](https://immich.app) library (albums, favorite status, description, GPS coordinates, capture date) into the EXIF / XMP / QuickTime tags of the underlying image and video files.

**Why this exists:** Immich stores metadata in its database, not in the files. When you add a photo to an album in Immich, the `.jpg` on disk is unchanged — so external tools (Lightroom, digiKam, PhotoPrism, plain `exiftool`) don't see your albums, ratings, or descriptions. This service bridges that gap by reading from the Immich API and writing to the files via ExifTool.

### How Immich Handles In-Place Metadata Updates (File Hashes)

Modifying embedded EXIF/IPTC/XMP tags updates the file's bytes and therefore changes its SHA-1 checksum in Immich. Because Immich tracks existing library and external-library assets by asset ID and file path rather than treating checksum changes as deletions, updating metadata in place does not cause assets to be lost or duplicated in your library—Immich uses the SHA-1 hash primarily for duplicate detection during initial upload/intake. (Note: if you later re-upload an already-modified file through a separate import flow, its updated hash will differ from the pre-edit original.)

## Data Sources

### Immich API
Used to retrieve metadata associated with assets, including:

- Album memberships
- Favorites
- Descriptions (including AI-generated descriptions)
- GPS coordinates
- Capture dates

The Immich API is used for **reading metadata only**. This project does not modify assets through the Immich API.

### Filesystem (Upload & External Libraries)
Provides direct access to the original media files on disk across both Immich-managed uploads and **External Libraries**.

ExifTool is used to write metadata directly into the media files in place. This requires the container to have **read/write access** to every volume containing assets you want updated.

The API provides the metadata; the filesystem provides the actual files to modify.

Features
--------

*   Incremental sync — only touches assets that changed in Immich
*   **Multi-user / multi-account support** — runs an independent sync cycle with isolated state files for each configured API key
*   **Native support for both Upload and External Libraries** — works out of the box when container mount paths mirror `immich-server`, or translates custom mount layouts via `PATH_MAPPINGS`
*   Per-file diff — only writes tags that actually changed
*   **Non-destructive keyword handling** — only removes keywords it wrote itself (verified against state); user-added keywords are never touched
*   Correct GPS hemisphere handling (`abs` + explicit `N/S/E/W` ref)
*   ISO 8601 → EXIF date conversion (refuses to write garbage)
*   JPEG-in-HEIC detection — safely stages `.jpg` writes and atomically preserves the original filename when a JPEG file has a `.heic`/`.heif`/`.hif` extension
*   Dry-run and backup modes for safe first runs
*   Atomic state writes; corrupt state is quarantined, not silently discarded
*   Graceful shutdown on SIGTERM/SIGINT (SIGKILL-proof via `tini`)
*   One persistent ExifTool subprocess for the whole sync (fast)

Requirements
------------

*   A running Immich instance and one or more API keys from **User Settings → API Keys**.  Note: you probably just need `asset.read` and `album.read` permissions for the API key.  Open an issue if you find errors. If you are able to test out different permissions and find a different minimum need, please tell me.  TBH I haven't tested this.
*   The Immich upload directory and any External Library directories bind-mounted into the container (read/write)
*   ExifTool ≥ 12.57 (tested through 13.68)

Quick start
-----------

### 1\. Create `compose.yml`

Minimum required:

    IMMICH_URL=http://immich-server:2283
    IMMICH_API_KEYS=your-api-key-here

By default, `immich-exif` resolves the `originalPath` reported by Immich directly on disk. Mounting your volumes at the **exact same container paths** used by `immich-server` (for example, `/usr/src/app/upload` and `/usr/src/app/external/...`) requires zero path configuration.

If your `immich-exif` container mounts those directories at different paths, map them with `PATH_MAPPINGS`:

    PATH_MAPPINGS=/usr/src/app/upload=/data/upload,/usr/src/app/external/photos=/data/external/photos

### 2\. Container volume setup

#### Option A: Mirror `immich-server` paths (recommended — zero path config)

**`docker-compose.yml` example:**

    services:
      immich-exif:
        build: .
        container_name: immich-exif
        restart: unless-stopped
        env_file: .env
        volumes:
          - /path/to/immich/upload:/usr/src/app/upload:rw
          - /path/to/external/photos:/usr/src/app/external/photos:rw
          - /path/to/external/videos:/usr/src/app/external/videos:rw
          - ./state:/app/state
          - ./logs:/app/logs

**Podman Quadlet (`.container`) example:**

    Volume=%h/Pictures:/usr/src/app/upload:rw
    Volume=/mnt/external_pictures:/usr/src/app/external/external_pictures:rw
    Volume=/mnt/external_videos:/usr/src/app/external/external_videos:rw

*(Never mount multiple different host volumes to the same container target path; give each volume its own non-overlapping path.)*

#### Option B: Custom container paths with `PATH_MAPPINGS`

If your container mounts volumes under custom paths (e.g., `/mnt/upload` and `/mnt/external_photos`), set `PATH_MAPPINGS` in `.env` or your container environment:

    PATH_MAPPINGS=/usr/src/app/upload=/mnt/upload,/usr/src/app/external/photos=/mnt/external_photos

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

## Configuration

All configuration is via environment variables (or a `.env` file next to the process). Real environment variables always override `.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMMICH_URL` | **Yes** | — | Immich base URL, e.g. `http://immich-server:2283`. Must start with `http://` or `https://`. |
| `IMMICH_API_KEYS` | **Yes†** | — | Comma-separated list of Immich API keys (one per user account). Each key runs an independent sync cycle with its own state file. |
| `IMMICH_API_KEY` | **Yes†** | — | Single API key fallback for installations using one Immich account. Used only if `IMMICH_API_KEYS` is not set. |
| `PATH_MAPPINGS` | No | — | Optional comma- or newline-separated `immich_prefix=local_prefix` pairs to translate `immich-server` container paths to paths in this container (e.g. `/usr/src/app/upload=/data/upload`). Not needed if mount paths match `immich-server`. |
| `SYNC_INTERVAL` | No | `900` | Seconds between sync cycles. `900` = 15 minutes. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `LOG_FILE` | No | `/app/logs/exif-writer.log` | Log file path. Logs grow forever if enabled. Delete as needed. |
| `STATE_FILE` | No | `/app/state/sync_state.json` | Base path where sync state is persisted. Additional accounts use `<name>.account-2.json`, `<name>.account-3.json`, etc. |
| `EXIF_DRY_RUN` | No | `0` | If `1`, log intended writes and modify nothing. The sync-state file is also left unchanged, so a later real run will still process the assets. |
| `EXIF_KEEP_BACKUPS` | No | `1` | If `1`, ExifTool saves `<file>_original` before editing. Set `0` only after you trust the tool. |
| `LOG_TO_FILE` | No | `1` | If `1`, writes logs to `LOG_FILE` and the console. If `0`, logs to the console only; no log file or log directory is required. |

† At least one of `IMMICH_API_KEYS` or `IMMICH_API_KEY` is required. If both are configured, `IMMICH_API_KEYS` takes precedence.

Startup fails fast with a clear `ValueError` if any required variable is missing, if a key is still the placeholder `YOUR_API_KEY_HERE`, if `PATH_MAPPINGS` contains a malformed `src=dst` entry, if `SYNC_INTERVAL` isn't a positive integer, or if `LOG_LEVEL` isn't a known level.

Multiple API keys (Multi-User Sync)
-----------------------------------

Provide one API key per Immich user account as a comma-separated list:

    IMMICH_API_KEYS=key_user1,key_user2

Behavior:

*   **Independent account sync:** Each API key represents a distinct Immich user identity. On every cycle, `immich-exif` runs a full incremental sync for each configured account in sequence so both user libraries and external libraries are updated.
*   **Per-account state isolation:** Account 1 uses `STATE_FILE` (default `/app/state/sync_state.json`), while subsequent accounts automatically write to `sync_state.account-2.json`, `sync_state.account-3.json`, and so on.
*   **Fault isolation:** If one account encounters an error, it is logged and skipped so remaining accounts still sync normally.
*   **Transient retries:** `5xx` and connection errors retry with exponential backoff and jitter.

Whitespace around keys is trimmed. Newlines in the env var are also accepted as separators, though single-line comma-separated values work best with `env_file:` parsers.

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

### Read-only video containers

Matroska (`.mkv`), WebM (`.webm`), and AVI (`.avi`) files are skipped cleanly for tag writes and recorded in state as handled so they do not generate repeated warnings on subsequent incremental runs.

### State schema & error handling

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
             /path/to/photo.jpg

For a video:

    exiftool -Subject -Rating -Description -CreateDate \
             /path/to/video.mp4

To see exactly what changed vs the backup:

    exiftool -diff photo.jpg photo.jpg_original

Troubleshooting
---------------

### Assets log "File missing"

The `WARNING` log line shows both `originalPath` (the path inside `immich-server`) and `resolved` (the path checked inside `immich-exif`). Compare them to diagnose:

1.  **Missing volume mount** — Make sure every upload and external library volume mounted into `immich-server` is also mounted read/write into `immich-exif`.
2.  **Mismatched container paths** — Either update your `immich-exif` volume mount target so it matches `originalPath` directly, or add a `PATH_MAPPINGS` rule translating the `immich-server` prefix to your `immich-exif` mount path.
3.  **Colliding mounts** — Ensure you haven't mounted two host directories onto the same target directory in your compose or Quadlet file.

### `401` / `403` authentication errors

Either an API key is wrong/expired, or it belongs to a user without access to those assets. Regenerate the user's API key in Immich (**User Settings → API Keys**) and update `IMMICH_API_KEYS`.

### Videos not getting keywords

Videos are tagged with `XMP:Subject`. Some players/DAM tools only read `QuickTime:Keywords` or `Keys:Keywords`. If your tool doesn't show video keywords, try `exiftool -XMP:Subject -a video.mp4` to confirm the tag is present; if it is, the issue is your tool's reader, not the writer.

### Photos suddenly have fewer keywords

Check the diff against the backup. If keywords were removed, they must have matched our previous album list. If they were user-added but happen to match an album name you've since removed, they'd be removed — this is the inherent trade-off of surgical diffing vs. never removing anything. If you want "add only, never remove" behavior, that's a one-line change in `_keyword_params` (remove the `to_remove` block); say so and I'll patch it.

### Logs show `[DRY RUN]` but you didn't set `EXIF_DRY_RUN`

Check that `EXIF_DRY_RUN=1` isn't set in your shell environment, your `docker-compose.yml` `environment:` block, your systemd Quadlet unit, or inherited from the parent process. `printenv | grep EXIF` shows what's in the current shell.

### Container takes a long time to shut down

If using `docker compose stop`, the process should exit within a few seconds because `tini` + SIGTERM handler + `stop_event.wait()` (not `time.sleep()`) means in-flight work is finished and the process exits cleanly. If it takes longer than ~10 seconds, check that you built with the new `Dockerfile` (which has `tini`). Without tini, Python's SIGTERM handler is still called, but child processes (ExifTool) can be orphaned.

ExifTool version note
---------------------

The provided `Dockerfile` builds ExifTool **13.64** from source (overriding Debian bookworm's 12.57) and verifies the installed version during the build. If you need a different version, change the `ARG EXIFTOOL_VERSION` line near the top of the `Dockerfile` and rebuild. To confirm the version in a built image:

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

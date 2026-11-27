immich-exif
===========

EXIF ↔ Immich metadata bridge

Overview [What & Why](#intro) [Data Sources](#data-sources) [Features](#features) [Requirements](#requirements) Setup [Quick Start](#quickstart) [Volume Setup](#volumes) [First Run](#first-run) Configuration [Core Variables](#config-core) [Database Variables](#config-db) [Multi-User Sync](#multi-user) Safety [Dry Run](#dry-run) [Backups](#backups) [Keyword Safety](#keyword-safety) [State & Errors](#state-errors) [Checksum Repair](#checksums) Reference [Verifying Writes](#verify) [Troubleshooting](#troubleshooting) [ExifTool Version](#exiftool-version) [Running Locally](#local-dev)

immich-exif
===========

Writes metadata from your [Immich](https://immich.app) library — albums, favorites, descriptions, GPS coordinates, capture dates — directly into the EXIF / XMP / QuickTime tags of the underlying image and video files.  
  
Forked from [jmathai/immich-exif](https://github.com/jmathai/immich-exif) and substantially rewritten. The original dev has reappeared — please [support his work](https://github.com/jmathai).

**AI Disclosure.** I am not a real programmer. I vibe-coded the crap out of this to fix bugs, add multi-account support, rework logging, and add env-var configuration. I've run it extensively on my own setup, but **take a backup before you run this** — just in case.

What & Why
----------

Immich stores metadata — album memberships, ratings, descriptions, GPS — in its database, not in the files themselves. When you add a photo to an album in Immich, the `.jpg` on disk is unchanged, so external tools (Lightroom, digiKam, PhotoPrism, plain `exiftool`) see none of it. This service bridges that gap: it reads from the Immich API and writes to the files via ExifTool.

**File hashes after a write.** Modifying embedded EXIF/IPTC/XMP tags changes the file's bytes and therefore its SHA-1 checksum. Immich tracks assets by asset ID and file path rather than checksum, so in-place metadata updates do _not_ cause assets to be lost or duplicated — Immich uses SHA-1 primarily for duplicate detection on initial upload. However, the stored checksum in the database becomes stale, and Immich will flag these under **Administration → Maintenance → Checksum mismatches**. See [Checksum Repair](#checksums) below for how this is handled automatically.

Data Sources
------------

### Immich API — read only

Used to retrieve asset metadata, including:

*   Album memberships
*   Favorites
*   Descriptions (including AI-generated descriptions)
*   GPS coordinates
*   Capture dates

This project does **not** modify assets through the Immich API.

### Filesystem (Upload & External Libraries)

Provides direct read/write access to the original media files on disk — both Immich-managed uploads and External Libraries. ExifTool writes metadata in place. The container must have **read/write access** to every volume containing assets you want updated.

### Postgres Database New

After each successful EXIF write, the new SHA-1 checksum is computed from the modified file and written directly to Immich's `assets` table via `psycopg` (Postgres v3 driver). This keeps Immich's database in sync without requiring a manual maintenance run. See [Database Variables](#config-db) and [Checksum Repair](#checksums).

Features
--------

Incremental sync — only touches changed assets

Multi-user / multi-account support with isolated state

Upload and External Library support, zero config when paths match

Per-file diff — only writes tags that actually changed

Non-destructive keyword handling — never removes user-added keywords

Correct GPS hemisphere handling (abs + explicit N/S/E/W ref)

ISO 8601 → EXIF date conversion; refuses garbage dates

JPEG-in-HEIC detection with atomic staged writes

Dry-run and backup modes for safe first runs

Atomic state writes; corrupt state quarantined, not discarded

Graceful SIGTERM/SIGINT shutdown via tini

Automatic checksum DB patch after each file write

Requirements
------------

*   A running Immich instance with one or more API keys from **User Settings → API Keys**. You likely only need `asset.read` and `album.read` permissions — open an issue if you discover otherwise.
*   The Immich upload directory and any External Library directories bind-mounted into the container with read/write access.
*   ExifTool ≥ 12.57 (tested through 13.68 — the provided Dockerfile builds 13.64 from source).
*   `psycopg[binary]` ≥ 3.1 — included in `requirements.txt`; required for automatic checksum repair.

Quick Start
-----------

### 1\. Minimum .env

Two variables are always required:

    IMMICH_URL=http://immich-server:2283
    IMMICH_API_KEYS=your-api-key-here

By default, `immich-exif` resolves the `originalPath` reported by Immich directly on disk. Mounting volumes at the **exact same container paths** used by `immich-server` (e.g. `/usr/src/app/upload`) requires zero path configuration. If your paths differ, map them with `PATH_MAPPINGS`:

    PATH_MAPPINGS=/usr/src/app/upload=/data/upload,/usr/src/app/external/photos=/data/external/photos

Volume Setup
------------

### Option A — Mirror immich-server paths (recommended)

Zero path configuration needed when container mount targets match `immich-server` exactly.

docker-compose.yml

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

Podman Quadlet (.container)

    Volume=%h/Pictures:/usr/src/app/upload:rw
    Volume=/mnt/external_pictures:/usr/src/app/external/external_pictures:rw
    Volume=/mnt/external_videos:/usr/src/app/external/external_videos:rw

**Never** mount multiple different host volumes to the same container target path. Give each volume its own non-overlapping path.

### Option B — Custom paths with PATH\_MAPPINGS

    PATH_MAPPINGS=/usr/src/app/upload=/mnt/upload,/usr/src/app/external/photos=/mnt/external_photos

First Run
---------

Always start with `EXIF_DRY_RUN=1`:

Docker

    docker compose build
    docker compose up -d
    docker compose logs -f immich-exif

Review the `[DRY RUN]` entries in the logs. No media files, sync state, or database rows are changed in dry-run mode. When the log looks right:

    # .env
    EXIF_DRY_RUN=0
    
    docker compose up -d --force-recreate

**Keep backups on for the first real run.** The first run after a dry-run may recheck the full library if the state file was created by an older version.

Configuration — Core Variables
------------------------------

All configuration is via environment variables or a `.env` file. Real environment variables always override `.env`. Startup fails fast with a clear `ValueError` if a required variable is missing, a key is still the placeholder `YOUR_API_KEY_HERE`, `PATH_MAPPINGS` contains a malformed entry, `SYNC_INTERVAL` is not a positive integer, or `LOG_LEVEL` is unknown.

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMMICH_URL` | **Yes** | — | Immich base URL, e.g. `http://immich-server:2283`. Must start with `http://` or `https://`. |
| `IMMICH_API_KEYS` | **Yes †** | — | Comma-separated list of Immich API keys (one per user account). Each key runs an independent sync cycle with its own state file. |
| `IMMICH_API_KEY` | **Yes †** | — | Single API key fallback for one-account installations. Used only if `IMMICH_API_KEYS` is not set. |
| `PATH_MAPPINGS` | No | — | Comma- or newline-separated `immich_prefix=local_prefix` pairs. Not needed if mount paths match `immich-server`. |
| `SYNC_INTERVAL` | No | `900` | Seconds between sync cycles. `900` = 15 minutes. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`. |
| `LOG_FILE` | No | `/app/logs/exif-writer.log` | Log file path. Logs grow indefinitely — delete as needed. |
| `LOG_TO_FILE` | No | `1` | If `1`, writes logs to `LOG_FILE` and the console. If `0`, console only; no log directory required. |
| `STATE_FILE` | No | `/app/state/sync_state.json` | Base path for sync state. Additional accounts write to `<name>.account-2.json`, `.account-3.json`, etc. |
| `EXIF_DRY_RUN` | No | `0` | If `1`, log all intended writes and modify nothing — files, state, and database are all left untouched. |
| `EXIF_KEEP_BACKUPS` | No | `1` | If `1`, ExifTool saves a `<file>_original` sibling before each edit. Set `0` only after you trust the tool. |
| `ENCODE_FACES` | No | `0` | If `1`, writes face data (including boundary information) to the file. |

† At least one of `IMMICH_API_KEYS` or `IMMICH_API_KEY` is required. If both are set, `IMMICH_API_KEYS` takes precedence.

Configuration — Database Variables New
--------------------------------------

After writing EXIF data to a file, `immich-exif` recomputes the file's SHA-1 and patches the `assets.checksum` column in Immich's Postgres database directly. This prevents the checksum mismatch warnings that would otherwise appear in Administration → Maintenance.

These variables mirror the ones already declared in `immich-server`'s own Quadlet unit or `docker-compose.yml` — no new secrets to manage. If `DB_PASSWORD` is absent, the checksum patch is skipped and a warning is logged per asset, but the EXIF write itself still succeeds.

Podman Quadlet — reuse immich-server's existing stanzas

    Environment=DB_USERNAME=postgres
    Secret=immich-db-password,type=env,target=DB_PASSWORD
    Environment=DB_DATABASE_NAME=immich
    Environment=DB_HOSTNAME=immich-postgres
    # DB_PORT defaults to 5432 — only set if yours differs

Variable

Required

Default

Description

`DB_USERNAME`

No

postgres

Postgres username. Same value as `immich-server`'s `DB_USERNAME`.

`DB_PASSWORD`

Yes \*

—

Postgres password. Use the same `Secret=` stanza as `immich-server` so there is one source of truth. If absent, checksum patching is skipped with a warning.

`DB_HOSTNAME`

No

immich-postgres

Postgres host. Same value as `immich-server`'s `DB_HOSTNAME`. The container must be on the same Podman network as `immich-postgres`.

`DB_DATABASE_NAME`

No

immich

Postgres database name. Same value as `immich-server`'s `DB_DATABASE_NAME`.

`DB_PORT`

No

5432

Postgres port. `immich-server` does not set this explicitly (it relies on the libpq default); only add it if your setup uses a non-standard port.

\* Required only for automatic checksum patching. EXIF writes proceed regardless.

**Network access.** The `immich-exif` container must be able to reach `immich-postgres` over the container network. In a Quadlet setup, add `Network=immich.network` (or whatever your shared network is named) to the `[Container]` section — the same network `immich-server` and `immich-postgres` share.

Multi-User Sync
---------------

Provide one API key per Immich user account as a comma-separated list:

    IMMICH_API_KEYS=key_user1,key_user2

*   **Independent account sync** — each key is a distinct Immich user identity; a full incremental sync runs for each account in sequence every cycle.
*   **Per-account state isolation** — Account 1 uses `STATE_FILE`; subsequent accounts write to `sync_state.account-2.json`, `.account-3.json`, etc.
*   **Fault isolation** — if one account errors, it is logged and skipped; remaining accounts sync normally.
*   **Transient retries** — 5xx and connection errors retry with exponential backoff and jitter.

Whitespace around keys is trimmed. Newlines are accepted as separators in addition to commas.

Safety — Dry Run
----------------

Set `EXIF_DRY_RUN=1` and every intended ExifTool command is logged at `INFO` with a `[DRY RUN]` prefix. Media files are not modified, asset metadata is not committed to the sync cache, the sync timestamp is not advanced, the state file is not saved, and **no database rows are updated**. You can inspect the output, disable dry-run, and re-run without losing any work.

Safety — Backups
----------------

With the default `EXIF_KEEP_BACKUPS=1`, ExifTool saves a sibling `<original-name>_original` before each modification:

    /photo/2023/IMG_1234.jpg           ← modified
    /photo/2023/IMG_1234.jpg_original  ← byte-for-byte original

After confirming things look right, remove backups with:

    find /path/to/immich/upload -name '*_original' -delete

Safety — Keyword Handling
-------------------------

Keyword writes are surgical, not destructive:

*   Reads current keywords from the file via ExifTool `get_tags`
*   Removes only keywords that (a) are on disk _and_ (b) were recorded as written by us on the previous run (from `STATE_FILE`)
*   Adds any new albums as keywords
*   Keywords added manually in Lightroom, digiKam, etc. are **never** removed

Safety — State & Error Handling
-------------------------------

*   The sync engine does not prune cached assets from an incremental search, since that search is not a complete inventory.
*   The sync timestamp is not advanced when a run is interrupted or any asset fails — those assets are retried on the next cycle.
*   If Immich returns an error reading any album, the album-map scan aborts the sync entirely rather than continuing with incomplete data.
*   Corrupt state files are renamed to `sync_state.json.corrupt-<timestamp>` and logged at `ERROR`.

Checksum Repair New
-------------------

Every successful EXIF write changes the file's bytes. After the write, `update_immich_checksum()` in `exif_writer.py` computes the new SHA-1 in pure Python (1 MiB chunks, `hashlib.sha1`) and issues:

    UPDATE assets SET checksum = decode('<sha1hex>', 'hex') WHERE id = '<asset_id>'

The connection is opened from the DB env vars above and closed immediately — or a shared `psycopg.Connection` can be passed from the sync engine to avoid reconnecting per asset. Dry-run mode logs the SQL that _would_ run and touches nothing.

**Bulk repair for existing mismatches.** If you have pre-existing checksum mismatches (from writes before this feature was added), export the mismatch CSV from **Administration → Maintenance → Checksum mismatches**, then run the repair script below to hash all affected files via `immich-server` and apply the `UPDATE` statements in one batch.

### Generate and apply a bulk repair SQL file

Step 1 — hash all mismatched files and write UPDATE statements

    python3 - <<'EOF'
    import csv, subprocess
    from pathlib import Path
    
    src_path = Path.home() / "checksum_mismatch.csv"   # export from Immich UI
    sql_path = Path.home() / "large_checksums.sql"
    
    statements, skipped, processed = [], 0, 0
    
    with src_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            processed += 1
            asset_id = (row.get("assetId") or "").strip()
            container_path = (row.get("path") or "").strip()
            if not asset_id or not container_path:
                skipped += 1; continue
    
            proc = subprocess.run(
                ["podman", "exec", "immich-server", "sha1sum", container_path],
                capture_output=True, text=True,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                skipped += 1; continue
    
            sha1_hex = proc.stdout.strip().split()[0]
            statements.append(
                f"UPDATE assets SET checksum = decode('{sha1_hex}', 'hex') WHERE id = '{asset_id}';"
            )
            if processed % 1000 == 0:
                print(f"  {processed} rows processed, {len(statements)} SQL ready, {skipped} skipped")
    
    sql_path.write_text("\n".join(statements) + "\n", encoding="utf-8")
    print(f"Done: {len(statements)} UPDATE statements → {sql_path} ({skipped} skipped)")
    EOF

Verification — spot check the output

    head -n 5 ~/large_checksums.sql && wc -l ~/large_checksums.sql

Step 2 — apply to Postgres

    podman exec -i immich-postgres psql -U postgres -d immich -f - \
      < ~/large_checksums.sql > ~/large_psql.log 2>&1

Step 3 — verify results

    grep -c "^UPDATE 1" ~/large_psql.log
    grep -iE "error|conflict" ~/large_psql.log | head -n 20

Then refresh **Administration → Maintenance → Checksum mismatches** in the Immich web UI to confirm the count drops to zero.

Verifying a File After a Run
----------------------------

Photo

    exiftool -Keywords -Subject -Rating -Description \
             -GPSLatitude -GPSLatitudeRef \
             -GPSLongitude -GPSLongitudeRef \
             -DateTimeOriginal \
             /path/to/photo.jpg

Video

    exiftool -Subject -Rating -Description -CreateDate \
             /path/to/video.mp4

Diff against backup

    exiftool -diff photo.jpg photo.jpg_original

Troubleshooting
---------------

### Assets log "File missing"

The `WARNING` line shows both `originalPath` (inside `immich-server`) and `resolved` (inside `immich-exif`). Compare them:

1.  **Missing volume mount** — every upload and external library volume in `immich-server` must also be mounted read/write in `immich-exif`.
2.  **Mismatched container paths** — update the mount target so it matches `originalPath`, or add a `PATH_MAPPINGS` rule.
3.  **Colliding mounts** — two different host directories mounted to the same container path will shadow each other.

### 401 / 403 authentication errors

The API key is wrong, expired, or belongs to a user without access to those assets. Regenerate the key in **User Settings → API Keys** and update `IMMICH_API_KEYS`.

### Videos not getting keywords

Videos are tagged with `XMP:Subject`. Some tools only read `QuickTime:Keywords`. Run `exiftool -XMP:Subject -a video.mp4` to confirm the tag is present — if it is, the issue is your tool's reader, not the writer.

### Photos suddenly have fewer keywords

Keywords were removed because they matched both the on-disk state and the previous album list in the state file. If a user-added keyword happens to match an album name you've since removed in Immich, it will be removed — that is the inherent trade-off of surgical diffing. For "add only, never remove" behaviour, remove the `to_remove` block from `_keyword_params` in `exif_writer.py`.

### Checksum mismatches still appear after a write

Check that `DB_PASSWORD` is set and that the container can reach `immich-postgres` on the same network. Look for `update_immich_checksum` in the logs — a `WARNING` means the row wasn't found; an `ERROR` means the DB connection failed. For bulk pre-existing mismatches, use the repair script in the [Checksum Repair](#checksums) section.

### Logs show \[DRY RUN\] but you didn't set EXIF\_DRY\_RUN

Check your shell environment, `docker-compose.yml` `environment:` block, or Quadlet unit for a stale `EXIF_DRY_RUN=1`. Run `printenv | grep EXIF` to see what's in scope.

### Container takes a long time to shut down

With `tini` in the Dockerfile, SIGTERM should flush in-flight work and exit within a few seconds. If it takes longer than ~10 seconds, the image was probably built without `tini`. Without it, Python's SIGTERM handler still fires, but ExifTool child processes can be orphaned.

ExifTool Version Note
---------------------

The provided `Dockerfile` builds ExifTool **13.64** from source, overriding Debian bookworm's 12.57, and verifies the installed version at build time. To use a different version, change the `ARG EXIFTOOL_VERSION` line near the top of the `Dockerfile` and rebuild. To confirm what's in a built image:

    docker run --rm <your-image> exiftool -ver

Running Locally (Without Docker)
--------------------------------

    python -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    # Requires exiftool on PATH: https://exiftool.org/install.html
    
    cp .env.example .env
    $EDITOR .env
    
    EXIF_DRY_RUN=1 python -m src.main

Requires `src/__init__.py` to exist for the package import to work (it should already be in the repo).

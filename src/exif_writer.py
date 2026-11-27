# EXIF writer using a persistent ExifTool subprocess.
#
# SAFETY-RELEVANT BEHAVIOR:
#   * EXIF_DRY_RUN=1 (default 0) — log every intended command, write nothing.
#   * EXIF_KEEP_BACKUPS=1 (default 1) — retain a <file>_original backup.
#   * Keywords: only keywords that are BOTH on disk AND in
#     previous_metadata['albums'] are removable. Anything else is preserved.
#   * GPS: written with abs(value) + explicit N/S/E/W Ref tags.
#   * Dates: ISO 8601 is normalized to EXIF 'YYYY:MM:DD HH:MM:SS'.
#   * JPEG data stored under a HEIC-family extension is staged as a .jpg
#     during metadata writes, then atomically put back under its original name.
#   * Non-.webp files containing RIFF/WebP data are skipped with a warning.
#   * Format mismatches or corrupt file headers rejected by ExifTool are
#     reported as skipped-unwriteable so they do not stall sync progress.
import hashlib
import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from exiftool import ExifToolHelper


logger = logging.getLogger(__name__)


PHOTO_EXTENSIONS = {
    '.jpg', '.jpeg', '.jpe',
    '.png',
    '.gif',
    '.webp',
    '.tiff', '.tif',
    '.heic', '.heif', '.hif',
    '.avif',
    '.jxl',
    '.dng',
    '.cr2', '.cr3',
    '.nef', '.nrw',
    '.arw', '.sr2',
    '.rw2',
    '.orf', '.ori',
    '.raf',
    '.pef',
    '.srw',
    '.x3f',
    '.erf',
    '.mef',
    '.mos',
    '.mrw',
    '.rwl',
    '.iiq',
    '.gpr',
    '.psd',
}

READ_ONLY_VIDEO_EXTENSIONS = {
    '.mkv',
    '.webm',
    '.avi',
    '.mpg',
    '.mpeg',
    '.m2ts',
    '.mts',
    '.ts',
}

VIDEO_EXTENSIONS = {
    '.mp4',
    '.mov', '.qt',
    '.m4v', '.m4a',
    '.3gp', '.3gpp',
    '.3g2', '.3gp2',
    *READ_ONLY_VIDEO_EXTENSIONS,
}

HEIC_FAMILY_EXTENSIONS = {'.heic', '.heif', '.hif'}


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in {'0', 'false', 'no', 'off', ''}


class ExifWriter:
    def __init__(self, dry_run: Optional[bool] = None,
                 keep_backups: Optional[bool] = None):
        self._dry_run = (_env_bool('EXIF_DRY_RUN', False)
                         if dry_run is None else dry_run)
        self._keep_backups = (_env_bool('EXIF_KEEP_BACKUPS', True)
                              if keep_backups is None else keep_backups)
        self._et: Optional[ExifToolHelper] = None
        self.last_write_skipped_format: bool = False
        if self._dry_run:
            logger.warning(
                "ExifWriter running in DRY-RUN mode — no files will be modified.")
        elif not self._keep_backups:
            logger.warning(
                "ExifWriter running WITHOUT backups — originals will be "
                "overwritten after successful staged writes.")

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def keep_backups(self) -> bool:
        return self._keep_backups

    # ----- lifecycle -----
    def _get_et(self) -> ExifToolHelper:
        if self._et is None:
            self._et = ExifToolHelper(
                common_args=['-m', '-charset', 'iptc=UTF8'])
        return self._et

    def close(self) -> None:
        if self._et is not None:
            try:
                self._et.terminate()
            except Exception as e:
                logger.debug("ExifTool terminate failed: %s", e)
            self._et = None

    # ----- public API -----
    @staticmethod
    def is_supported_file(file_path: str) -> bool:
        if not os.path.exists(file_path):
            return False
        ext = os.path.splitext(file_path.lower())[1]
        return ext in PHOTO_EXTENSIONS or ext in VIDEO_EXTENSIONS

    @staticmethod
    def is_video(file_path: str) -> bool:
        return os.path.splitext(file_path.lower())[1] in VIDEO_EXTENSIONS

    @staticmethod
    def is_read_only_video(file_path: str) -> bool:
        return os.path.splitext(file_path.lower())[1] in READ_ONLY_VIDEO_EXTENSIONS

    @staticmethod
    def is_mislabeled_webp(file_path: str) -> bool:
        """Return True if a non-.webp file actually contains a RIFF/WebP container."""
        ext = os.path.splitext(file_path.lower())[1]
        if ext == '.webp':
            return False
        try:
            with open(file_path, 'rb') as f:
                header = f.read(12)
            return (
                len(header) >= 12
                and header[:4] == b'RIFF'
                and header[8:12] == b'WEBP'
            )
        except OSError as e:
            logger.warning("Could not inspect file header for %s: %s", file_path, e)
            return False

    @staticmethod
    def _is_jpeg_content(file_path: str) -> bool:
        """Identify JPEG data by its file signature, independent of extension."""
        try:
            with open(file_path, 'rb') as image_file:
                return image_file.read(3) == b'\xff\xd8\xff'
        except OSError as e:
            logger.warning("Could not inspect image signature for %s: %s",
                           file_path, e)
            return False

    def _needs_jpeg_extension_override(self, file_path: str) -> bool:
        ext = os.path.splitext(file_path.lower())[1]
        return (
            ext in HEIC_FAMILY_EXTENSIONS
            and self._is_jpeg_content(file_path)
        )

    @staticmethod
    def _is_format_or_structure_rejection(stderr_text: Optional[str]) -> bool:
        """Detect ExifTool errors caused by mislabeled formats or corrupt headers.

        Covers both hard format mismatches (wrong magic bytes, unknown type) and
        structural corruption that ExifTool cannot repair: truncated IFD/directory
        chains, unreadable image data blocks, etc.  Any match here causes the file
        to be skipped rather than logged as an actionable ERROR.
        """
        if not stderr_text:
            return False
        lower = stderr_text.lower()
        markers = (
            'not a valid ',
            'looks more like a ',
            'bad format ',
            'format error in file',
            'error reading file',
            # ExifTool emits "Error reading image data" for corrupt TIFF/JPEG blobs
            'error reading image data',
            'unknown file type',
            'corrupt ',
            # Truncated IFD chains (e.g. "Truncated ExifIFD directory")
            'truncated ',
        )
        return any(marker in lower for marker in markers)

    @staticmethod
    def _is_unchanged_without_error(
        result: Optional[str], stderr_text: Optional[str]
    ) -> bool:
        """Return True when ExifTool reports 0 updated / unchanged with no error."""
        if stderr_text and stderr_text.strip():
            return False
        if not result or not isinstance(result, str):
            return False
        lower = result.lower()
        return '0 image files updated' in lower and 'unchanged' in lower

    def write_metadata(self, file_path: str, metadata: Dict[str, Any],
                       previous_metadata: Optional[Dict[str, Any]] = None) -> bool:
        self.last_write_skipped_format = False

        if not self.is_supported_file(file_path):
            # Unsupported extension (e.g. .bmp) — not an error, just a skip.
            # Set the flag so callers (sync_engine) can distinguish "skipped
            # intentionally" from "tried and failed".
            self.last_write_skipped_format = True
            logger.debug("Unsupported file type, skipping: %s", file_path)
            return False

        if self.is_read_only_video(file_path):
            logger.info(
                "Metadata write skipped (read-only video container): %s",
                file_path,
            )
            return True

        if self.is_mislabeled_webp(file_path):
            logger.warning(
                "Metadata write skipped (RIFF/WebP file mislabeled as %s): %s",
                os.path.splitext(file_path)[1] or "<no ext>",
                file_path,
            )
            return True

        is_video_file = self.is_video(file_path)
        previous = previous_metadata or {}

        params = self._build_params(file_path, metadata, previous, is_video_file)
        tag_dict = self._build_tag_dict(metadata, previous, is_video_file)

        if not params and not tag_dict:
            logger.debug("No EXIF changes needed for %s", file_path)
            return True

        if self._dry_run:
            if params:
                logger.info("[DRY RUN] exiftool %s %s", params, file_path)
            if tag_dict:
                logger.info("[DRY RUN] exiftool set_tags %s %s", tag_dict, file_path)
            if self._needs_jpeg_extension_override(file_path):
                logger.info(
                    "[DRY RUN] Would stage JPEG content under a temporary .jpg "
                    "name while preserving original path: %s",
                    file_path,
                )
            return True

        if self._needs_jpeg_extension_override(file_path):
            return self._write_jpeg_with_extension_override(
                file_path, params, tag_dict
            )

        return self._perform_write_passes(
            file_path, params, tag_dict, force_overwrite=not self._keep_backups
        )

    # ----- Immich checksum repair -----

    @staticmethod
    def _build_pg_dsn() -> str:
        """Build a libpq connection string from Immich's standard env vars.

        Immich's own container environment exposes:
            DB_USERNAME       (default: postgres)
            DB_PASSWORD       (required)
            DB_HOSTNAME       (default: immich-postgres)
            DB_DATABASE_NAME  (default: immich)
            DB_PORT           (default: 5432)

        These are the same variables declared in the Quadlet unit's
        ``Environment=`` / ``Secret=`` stanzas, so no extra configuration is
        needed — just drop the exif container on the same Podman network.

        Raises:
            EnvironmentError: when DB_PASSWORD is absent or empty.
        """
        user     = os.environ.get('DB_USERNAME',      'postgres').strip()
        password = os.environ.get('DB_PASSWORD',      '').strip()
        host     = os.environ.get('DB_HOSTNAME',      'immich-postgres').strip()
        dbname   = os.environ.get('DB_DATABASE_NAME', 'immich').strip()
        port     = os.environ.get('DB_PORT',          '5432').strip()

        if not password:
            raise EnvironmentError(
                "DB_PASSWORD is not set or empty.  "
                "Add it to your Quadlet unit as a Secret= or Environment= "
                "stanza (the same value used by immich-server)."
            )

        # psycopg (v3) accepts standard libpq DSN strings.
        # Percent-encode special characters in the password so the URI
        # parser does not misinterpret them.
        from urllib.parse import quote as _urlquote
        return (
            f"postgresql://{_urlquote(user, safe='')}:{_urlquote(password, safe='')}"
            f"@{host}:{port}/{_urlquote(dbname, safe='')}"
        )

    def update_immich_checksum(
        self,
        file_path: str,
        asset_id: str,
        db_conn=None,
    ) -> bool:
        """Recompute the SHA-1 of *file_path* and patch Immich's asset.checksum.

        Writing EXIF data changes the file's bytes, which invalidates the
        checksum stored in Immich's database and causes mismatch errors under
        Administration → Maintenance → Checksum mismatches.  Call this
        immediately after a successful :meth:`write_metadata` to keep the DB
        in sync.

        Args:
            file_path:
                Absolute path to the file that was just written.
            asset_id:
                Immich asset UUID — the ``id`` column in the ``assets`` table.
            db_conn:
                An open ``psycopg.Connection`` (v3).  When *None* a
                short-lived connection is opened using credentials assembled
                from the standard Immich env vars (``DB_USERNAME``,
                ``DB_PASSWORD``, ``DB_HOSTNAME``, ``DB_DATABASE_NAME``,
                ``DB_PORT``) and closed before returning.  Pass a shared
                connection from the caller to avoid reconnecting on every
                asset — the method manages its own cursor and commit.

        Returns:
            ``True``  — checksum column updated (or dry-run logged).
            ``False`` — skipped or failed; details logged at WARNING / ERROR.

        Environment variables (when *db_conn* is None):
            ``DB_USERNAME``       Postgres user     (default: ``postgres``)
            ``DB_PASSWORD``       Postgres password (required)
            ``DB_HOSTNAME``       Postgres host     (default: ``immich-postgres``)
            ``DB_DATABASE_NAME``  Database name     (default: ``immich``)
            ``DB_PORT``           Postgres port     (default: ``5432``)
        """
        if not asset_id:
            logger.warning(
                "update_immich_checksum: asset_id is empty for %s — skipped",
                file_path,
            )
            return False

        # --- compute new SHA-1 from the modified file ---
        try:
            sha1 = hashlib.sha1(usedforsecurity=False)
            with open(file_path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b''):
                    sha1.update(chunk)
            sha1_hex = sha1.hexdigest()
        except OSError as exc:
            logger.error(
                "update_immich_checksum: cannot read %s to compute hash: %s",
                file_path, exc,
            )
            return False

        if self._dry_run:
            logger.info(
                "[DRY RUN] Would UPDATE assets SET checksum = decode('%s', 'hex') "
                "WHERE id = '%s'  (file: %s)",
                sha1_hex, asset_id, file_path,
            )
            return True

        # --- update the database ---
        sql = "UPDATE assets SET checksum = decode(%s, 'hex') WHERE id = %s"
        owned_conn = False
        conn = db_conn
        try:
            if conn is None:
                try:
                    import psycopg  # type: ignore[import]
                except ImportError:
                    logger.error(
                        "update_immich_checksum: psycopg (v3) is not installed; "
                        "cannot update checksum for asset %s.  "
                        "Install with: pip install 'psycopg[binary]'",
                        asset_id,
                    )
                    return False

                try:
                    dsn = self._build_pg_dsn()
                except EnvironmentError as exc:
                    logger.error(
                        "update_immich_checksum: %s — skipping checksum update "
                        "for asset %s (file: %s)",
                        exc, asset_id, file_path,
                    )
                    return False

                conn = psycopg.connect(dsn)
                owned_conn = True

            with conn.cursor() as cur:
                cur.execute(sql, (sha1_hex, asset_id))
                rows = cur.rowcount

            conn.commit()

            if rows == 0:
                logger.warning(
                    "update_immich_checksum: UPDATE matched 0 rows — "
                    "asset_id %s may not exist in the database (file: %s)",
                    asset_id, file_path,
                )
                return False

            logger.info(
                "Immich checksum updated: asset=%s sha1=%s file=%s",
                asset_id, sha1_hex, file_path,
            )
            return True

        except Exception as exc:
            logger.error(
                "update_immich_checksum: DB update failed for asset %s "
                "(file: %s): %s",
                asset_id, file_path, exc,
            )
            try:
                conn.rollback()
            except Exception:
                pass
            return False

        finally:
            if owned_conn and conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _write_jpeg_with_extension_override(
        self,
        file_path: str,
        params: List[str],
        tag_dict: Dict[str, Any],
    ) -> bool:
        directory = os.path.dirname(file_path) or '.'
        temp_path: Optional[str] = None

        try:
            fd, temp_path = tempfile.mkstemp(
                prefix='.immich-exif-', suffix='.jpg', dir=directory
            )
            os.close(fd)

            shutil.copy2(file_path, temp_path)

            logger.info(
                "Detected JPEG content with HEIC-family extension; staging "
                "metadata write as JPEG: %s -> %s",
                file_path, temp_path,
            )

            if not self._perform_write_passes(
                temp_path, params, tag_dict, force_overwrite=True
            ):
                if not self.last_write_skipped_format:
                    logger.error(
                        "Staged JPEG metadata write failed; original left untouched: %s",
                        file_path,
                    )
                return False

            if self._keep_backups:
                backup_path = file_path + '_original'
                shutil.copy2(file_path, backup_path)
                logger.info("Original file backup created: %s", backup_path)

            os.replace(temp_path, file_path)
            temp_path = None
            logger.info(
                "JPEG metadata write committed; original filename preserved: %s",
                file_path,
            )
            return True

        except Exception:
            logger.exception(
                "Could not complete staged JPEG write for %s; "
                "original file was not intentionally modified",
                file_path,
            )
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError as e:
                    logger.warning(
                        "Could not remove temporary file %s: %s", temp_path, e
                    )

    def _perform_write_passes(
        self,
        file_path: str,
        params: List[str],
        tag_dict: Dict[str, Any],
        force_overwrite: bool,
    ) -> bool:
        """Run the existing scalar-tag and multiline-description write passes."""
        ok = True

        if params:
            cli_params = list(params)
            if force_overwrite:
                cli_params = ['-overwrite_original'] + cli_params
            cli_params.append(file_path)

            try:
                logger.debug("ExifTool execute %s", cli_params)
                result = self._get_et().execute(*cli_params)
            except Exception as e:
                stderr_val = self._extract_stderr(e)
                if self._is_format_or_structure_rejection(stderr_val):
                    self.last_write_skipped_format = True
                    logger.warning(
                        "Skipping unwriteable/mislabeled file %s: %s",
                        file_path,
                        stderr_val.strip(),
                    )
                    return False
                self._log_execute_exception(
                    operation="metadata write",
                    file_path=file_path,
                    cli_params=cli_params,
                    exception=e,
                )
                ok = False
            else:
                stderr_val = getattr(self._get_et(), '_last_stderr', None)
                if self._is_unchanged_without_error(result, stderr_val):
                    self.last_write_skipped_format = True
                    logger.warning(
                        "Skipping file left unchanged by ExifTool %s: %s",
                        file_path,
                        str(result).strip(),
                    )
                    return False
                if self._is_failure(result):
                    if self._is_format_or_structure_rejection(stderr_val):
                        self.last_write_skipped_format = True
                        logger.warning(
                            "Skipping unwriteable/mislabeled file %s: %s",
                            file_path,
                            str(stderr_val).strip(),
                        )
                        return False
                    logger.error(
                        "ExifTool reported failure for %s — stdout: %r stderr: %r",
                        file_path,
                        result if isinstance(result, str) else None,
                        stderr_val,
                    )
                    ok = False

        if tag_dict and ok:
            cli_params: List[str] = []
            try:
                description = next(iter(tag_dict.values()))
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', newline='', suffix='.txt'
                ) as description_file:
                    description_file.write(description)
                    description_file.flush()

                    if force_overwrite:
                        cli_params.append('-overwrite_original')
                    for tag in tag_dict:
                        cli_params.append(f'-{tag}<={description_file.name}')
                    cli_params.append(file_path)

                    logger.debug(
                        "ExifTool description write using temporary file %s "
                        "for tags %s on %s",
                        description_file.name, list(tag_dict), file_path,
                    )
                    result = self._get_et().execute(*cli_params)
                    stderr_val = getattr(self._get_et(), '_last_stderr', None)
                    if self._is_unchanged_without_error(result, stderr_val):
                        self.last_write_skipped_format = True
                        logger.warning(
                            "Skipping file left unchanged by ExifTool on "
                            "description write %s: %s",
                            file_path,
                            str(result).strip(),
                        )
                        return False
                    if self._is_failure(result):
                        if self._is_format_or_structure_rejection(stderr_val):
                            self.last_write_skipped_format = True
                            logger.warning(
                                "Skipping unwriteable/mislabeled file on description "
                                "write %s: %s",
                                file_path,
                                str(stderr_val).strip(),
                            )
                            return False
                        logger.error(
                            "ExifTool description write failed for %s — "
                            "stdout: %r stderr: %r",
                            file_path, result,
                            stderr_val,
                        )
                        ok = False
            except Exception as e:
                stderr_val = self._extract_stderr(e)
                if self._is_format_or_structure_rejection(stderr_val):
                    self.last_write_skipped_format = True
                    logger.warning(
                        "Skipping unwriteable/mislabeled file on description "
                        "write %s: %s",
                        file_path,
                        stderr_val.strip(),
                    )
                    return False
                self._log_execute_exception(
                    operation="description write",
                    file_path=file_path,
                    cli_params=cli_params,
                    exception=e,
                    tags=list(tag_dict),
                )
                ok = False

        if ok:
            logger.debug("EXIF updated for %s", file_path)
        return ok

    def _extract_stderr(self, exception: Exception) -> Optional[str]:
        stderr = getattr(exception, 'stderr', None)
        if stderr is None and self._et is not None:
            stderr = getattr(self._et, '_last_stderr', None)
        if stderr is None:
            return None
        return str(stderr)

    # ----- ExifTool exception diagnostics -----
    def _log_execute_exception(
        self,
        operation: str,
        file_path: str,
        cli_params: List[str],
        exception: Exception,
        tags: Optional[List[str]] = None,
    ) -> None:
        et = self._et
        process_status = getattr(et, '_last_status', None)
        process_stdout = getattr(et, '_last_stdout', None)
        process_stderr = getattr(et, '_last_stderr', None)

        status = getattr(exception, 'status', None)
        stdout = getattr(exception, 'stdout', None)
        stderr = getattr(exception, 'stderr', None)

        if status is None:
            status = process_status
        if stdout is None:
            stdout = process_stdout
        if stderr is None:
            stderr = process_stderr

        logger.error(
            "ExifTool %s raised on %s: %s",
            operation, file_path, exception,
        )
        logger.error(
            "ExifTool failure diagnostics for %s — "
            "exit status: %r; stdout: %r; stderr: %r; "
            "exception type: %s; exception args: %r; "
            "CLI arguments: %r; tags: %r",
            file_path, status, stdout, stderr,
            type(exception).__name__, exception.args, cli_params, tags,
        )

    # ----- output inspection -----
    @staticmethod
    def _is_failure(result: Optional[str]) -> bool:
        if result is None:
            return False
        s = result.strip()
        if not s:
            return False

        lower = s.lower()
        if "weren't updated" in lower or 'could not be' in lower:
            return True

        if 'updated' in lower:
            for token in lower.replace('\n', ' ').split():
                if token.isdigit():
                    return int(token) == 0

        if 'unchanged' in lower:
            return False

        if 'error' in lower:
            return True

        return False

    # ----- parameter building -----
    def _build_params(self, file_path: str, metadata: Dict[str, Any],
                      previous: Dict[str, Any], is_video: bool) -> List[str]:
        params: List[str] = []
        params.extend(self._keyword_params(file_path, metadata, previous, is_video))
        params.extend(self._favorite_params(metadata, previous, is_video))
        params.extend(self._location_params(metadata, previous))
        params.extend(self._datetime_params(metadata, previous, is_video))
        return params

    def _build_tag_dict(self, metadata: Dict[str, Any], previous: Dict[str, Any],
                        is_video: bool) -> Dict[str, Any]:
        cur = metadata.get('description', '') or ''
        prev = previous.get('description', '') or ''
        if cur == prev:
            return {}
        if is_video:
            return {'QuickTime:Description': cur}
        return {'EXIF:ImageDescription': cur, 'XMP:Description': cur}

    @staticmethod
    def _sanitize_arg_value(val: Any) -> str:
        if val is None:
            return ''
        return ' '.join(str(val).splitlines()).strip()

    def _keyword_params(self, file_path: str, metadata: Dict[str, Any],
                        previous: Dict[str, Any], is_video: bool) -> List[str]:
        current = [
            cleaned for item in (metadata.get('albums') or [])
            if (cleaned := self._sanitize_arg_value(item))
        ]
        prev = [
            cleaned for item in (previous.get('albums') or [])
            if (cleaned := self._sanitize_arg_value(item))
        ]

        if current == prev:
            return []

        tag = 'XMP:Subject' if is_video else 'IPTC:Keywords'
        existing = self._read_keywords(file_path, tag)

        prev_set = set(prev)
        cur_set = set(current)
        exist_set = set(existing)

        ours_on_disk = exist_set & prev_set
        to_remove = sorted(ours_on_disk - cur_set)
        to_add = sorted(cur_set - exist_set)

        params: List[str] = []
        for kw in to_remove:
            clean_kw = self._sanitize_arg_value(kw)
            if clean_kw:
                params.append(f'-{tag}-={clean_kw}')
        for kw in to_add:
            clean_kw = self._sanitize_arg_value(kw)
            if clean_kw:
                params.append(f'-{tag}+={clean_kw}')
        return params

    def _read_keywords(self, file_path: str, tag: str) -> List[str]:
        tags_to_read = [tag]
        if tag == 'XMP:Subject':
            tags_to_read.append('XMP-dc:Subject')

        try:
            result = self._get_et().get_tags(file_path, tags=tags_to_read)
        except Exception as e:
            logger.warning("Could not read %s from %s: %s", tag, file_path, e)
            return []
        if not result:
            return []
        row = result[0]

        candidates = tags_to_read + [t.split(':', 1)[-1] for t in tags_to_read]
        seen: set[str] = set()
        keywords: List[str] = []
        for candidate in candidates:
            value = row.get(candidate)
            if value is None:
                continue
            if isinstance(value, str):
                items = [value] if value else []
            else:
                try:
                    items = [str(v) for v in value if v]
                except TypeError:
                    items = [str(value)]
            for item in items:
                if item not in seen:
                    seen.add(item)
                    keywords.append(item)
        return keywords

    @staticmethod
    def _favorite_params(metadata: Dict[str, Any], previous: Dict[str, Any],
                         is_video: bool) -> List[str]:
        cur = bool(metadata.get('is_favorite', False))
        prev = bool(previous.get('is_favorite', False))
        if cur == prev:
            return []
        tag = 'QuickTime:Rating' if is_video else 'XMP:Rating'
        return [f'-{tag}=5' if cur else f'-{tag}=']

    @staticmethod
    def _location_params(metadata: Dict[str, Any],
                         previous: Dict[str, Any]) -> List[str]:
        lat, lon = metadata.get('latitude'), metadata.get('longitude')
        p_lat, p_lon = previous.get('latitude'), previous.get('longitude')
        if lat == p_lat and lon == p_lon:
            return []

        if lat is not None and lon is not None:
            try:
                lat_f, lon_f = float(lat), float(lon)
            except (TypeError, ValueError):
                logger.warning("Invalid GPS values lat=%r lon=%r; skipping",
                               lat, lon)
                return []
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                logger.warning("GPS out of range lat=%r lon=%r; skipping",
                               lat, lon)
                return []
            lat_ref = 'N' if lat_f >= 0 else 'S'
            lon_ref = 'E' if lon_f >= 0 else 'W'
            return [
                f'-GPS:GPSLatitude={abs(lat_f)}',
                f'-GPS:GPSLatitudeRef={lat_ref}',
                f'-GPS:GPSLongitude={abs(lon_f)}',
                f'-GPS:GPSLongitudeRef={lon_ref}',
            ]

        return [
            '-GPS:GPSLatitude=', '-GPS:GPSLatitudeRef=',
            '-GPS:GPSLongitude=', '-GPS:GPSLongitudeRef=',
        ]

    @staticmethod
    def _normalize_datetime(raw: str) -> Tuple[str, Optional[str]]:
        if not raw:
            return '', None
        s = str(raw).strip()

        try:
            datetime.strptime(s, '%Y:%m:%d %H:%M:%S')
            return s, None
        except ValueError:
            pass

        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            exif_str = dt.strftime('%Y:%m:%d %H:%M:%S')
            offset: Optional[str] = None
            if dt.utcoffset() is not None:
                total_secs = int(dt.utcoffset().total_seconds())
                sign = '+' if total_secs >= 0 else '-'
                h, remainder = divmod(abs(total_secs), 3600)
                m = remainder // 60
                offset = f'{sign}{h:02d}:{m:02d}'
            return exif_str, offset
        except ValueError:
            logger.warning(
                "Cannot normalize datetime %r; skipping date write", raw)
            return '', None

    def _datetime_params(self, metadata: Dict[str, Any], previous: Dict[str, Any],
                         is_video: bool) -> List[str]:
        cur_raw = metadata.get('date_time', '') or ''
        prev_raw = previous.get('date_time', '') or ''
        if cur_raw == prev_raw:
            return []

        cur, offset = self._normalize_datetime(cur_raw)
        if cur_raw and not cur:
            return []

        if is_video:
            if cur:
                return [
                    f'-QuickTime:CreateDate={cur}',
                    f'-QuickTime:ModifyDate={cur}',
                ]
            return [
                '-QuickTime:CreateDate=',
                '-QuickTime:ModifyDate=',
            ]

        if cur:
            params = [f'-EXIF:DateTimeOriginal={cur}', f'-EXIF:CreateDate={cur}']
            if offset:
                params += [f'-EXIF:OffsetTimeOriginal={offset}',
                           f'-EXIF:OffsetTime={offset}']
            return params
        return ['-EXIF:DateTimeOriginal=', '-EXIF:CreateDate=',
                '-EXIF:OffsetTimeOriginal=', '-EXIF:OffsetTime=']

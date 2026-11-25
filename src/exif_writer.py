
# EXIF writer using a persistent ExifTool subprocess.
#
# SAFETY-RELEVANT BEHAVIOR:
#   * EXIF_DRY_RUN=1 (default 0) — log every intended command, write nothing.
#   * EXIF_KEEP_BACKUPS=1 (default 1) — ExifTool keeps a <file>_original copy.
#     Set to 0 only after you trust the tool.
#   * Keywords: only keywords that are BOTH on disk AND in
#     previous_metadata['albums'] are removable. Anything else is presumed
#     user-authored and is preserved.
#   * GPS: written with abs(value) + explicit N/S/E/W Ref tags so the sign
#     can never be lost.
#   * Dates: ISO 8601 is normalized to EXIF 'YYYY:MM:DD HH:MM:SS'.
#     Unparseable input is skipped (never written as garbage).
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from exiftool import ExifToolHelper


logger = logging.getLogger(__name__)


# Extensions where exiftool supports WRITING metadata (R/W in the format table
# at https://exiftool.org/index.html), filtered to formats Immich ingests.
# Source: exiftool 13.64 format table, cross-referenced with Immich's supported
# media list. Read-only formats (3FR, DCR, KDC, etc.) are intentionally excluded
# — exiftool would open them but refuse to write, producing silent no-ops.
PHOTO_EXTENSIONS = {
    # Common consumer formats
    '.jpg', '.jpeg', '.jpe',
    '.png',
    '.gif',
    '.webp',
    '.tiff', '.tif',
    '.heic', '.heif', '.hif',
    '.avif',
    '.jxl',                        # JPEG XL — exiftool R/W since ~12.50
    # RAW formats (all R/W in exiftool)
    '.dng',
    '.cr2', '.cr3',                # Canon
    '.nef', '.nrw',                # Nikon
    '.arw', '.sr2',                # Sony
    '.rw2',                        # Panasonic
    '.orf', '.ori',                # Olympus
    '.raf',                        # Fujifilm
    '.pef',                        # Pentax
    '.srw',                        # Samsung
    '.x3f',                        # Sigma/Foveon
    '.erf',                        # Epson
    '.mef',                        # Mamiya
    '.mos',                        # Leaf/Creo
    '.mrw',                        # Minolta
    '.rwl',                        # Leica
    '.iiq',                        # Phase One
    '.gpr',                        # GoPro RAW
}

# Video formats Immich may contain.
# ExifTool can write metadata to the first group, but the installed ExifTool
# does not list MKV, WEBM, or AVI as writable formats. Those are handled as
# read-only assets and skipped by write_metadata() without invoking ExifTool.
READ_ONLY_VIDEO_EXTENSIONS = {
    '.mkv',
    '.webm',
    '.avi',
}

VIDEO_EXTENSIONS = {
    '.mp4',
    '.mov', '.qt',
    '.m4v', '.m4a',
    '.3gp', '.3gpp',
    '.3g2', '.3gp2',
    *READ_ONLY_VIDEO_EXTENSIONS,
}


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
        if self._dry_run:
            logger.warning(
                "ExifWriter running in DRY-RUN mode — no files will be modified.")
        elif not self._keep_backups:
            logger.warning(
                "ExifWriter running WITHOUT backups — ExifTool will overwrite "
                "originals in place. Set EXIF_KEEP_BACKUPS=1 to restore.")

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

    def write_metadata(self, file_path: str, metadata: Dict[str, Any],
                       previous_metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_supported_file(file_path):
            logger.warning("Unsupported file type: %s", file_path)
            return False

        # The installed ExifTool does not support writing these containers.
        # Avoid generating impossible QuickTime tag writes and treat the asset as
        # a handled no-op so it does not become a recurring write error.
        if self.is_read_only_video(file_path):
            logger.info(
                "Metadata write skipped (read-only video container): %s",
                file_path,
            )
            return True

        is_video_file = self.is_video(file_path)
        previous = previous_metadata or {}

        # Split into two write passes:
        #   1. params — scalar/simple tags via execute() (keywords, GPS, dates,
        #               favorites). Safe as CLI args once sanitized.
        #   2. tag_dict — descriptions written from a temporary UTF-8 text file
        #               using ExifTool's -TAG<=FILE syntax. This safely preserves
        #               multiline captions without passing their text as arguments.
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
            return True

        ok = True

        # Pass 1: CLI-safe params via execute().
        if params:
            cli_params = list(params)
            if not self._keep_backups:
                cli_params = ['-overwrite_original'] + cli_params
            cli_params.append(file_path)
            try:
                logger.debug("ExifTool execute %s", cli_params)
                result = self._get_et().execute(*cli_params)
            except Exception as e:
                self._log_execute_exception(
                    operation="metadata write",
                    file_path=file_path,
                    cli_params=cli_params,
                    exception=e,
                )
                ok = False
            else:
                if self._is_failure(result):
                    logger.error(
                        "ExifTool reported failure for %s — stdout: %r  stderr: %r",
                        file_path,
                        result if isinstance(result, str) else None,
                        getattr(self._get_et(), '_last_stderr', None))
                    ok = False

        # Pass 2: descriptions via a temporary UTF-8 file. ExifTool's
        # -TAG<=FILE form reads the value directly and safely preserves newlines.
        if tag_dict and ok:
            cli_params: List[str] = []
            try:
                description = next(iter(tag_dict.values()))
                with tempfile.NamedTemporaryFile(
                    mode='w', encoding='utf-8', newline='', suffix='.txt'
                ) as description_file:
                    description_file.write(description)
                    description_file.flush()

                    if not self._keep_backups:
                        cli_params.append('-overwrite_original')
                    for tag in tag_dict:
                        cli_params.append(f'-{tag}<={description_file.name}')
                    cli_params.append(file_path)

                    logger.debug(
                        "ExifTool description write using temporary file %s "
                        "for tags %s on %s",
                        description_file.name, list(tag_dict), file_path)
                    result = self._get_et().execute(*cli_params)
                    if self._is_failure(result):
                        logger.error(
                            "ExifTool description write failed for %s — "
                            "stdout: %r stderr: %r",
                            file_path, result,
                            getattr(self._get_et(), '_last_stderr', None))
                        ok = False
            except Exception as e:
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

    # ----- ExifTool exception diagnostics -----
    def _log_execute_exception(
        self,
        operation: str,
        file_path: str,
        cli_params: List[str],
        exception: Exception,
        tags: Optional[List[str]] = None,
    ) -> None:
        """
        Log the actual ExifTool process diagnostics when execute() raises.

        ExifToolHelper.execute() raises ExifToolExecuteError for a non-zero
        exit status. The helper stores the process output on its ExifTool
        instance, so capture those values before another command can replace
        them.
        """
        et = self._et

        # PyExifTool stores these on the persistent ExifTool process object.
        process_status = getattr(et, '_last_status', None)
        process_stdout = getattr(et, '_last_stdout', None)
        process_stderr = getattr(et, '_last_stderr', None)

        # Some PyExifTool versions also expose the same values on the
        # exception object. Prefer those when present, otherwise use the
        # process object's saved values.
        exception_status = getattr(exception, 'status', None)
        exception_stdout = getattr(exception, 'stdout', None)
        exception_stderr = getattr(exception, 'stderr', None)

        status = (
            exception_status
            if exception_status is not None
            else process_status
        )
        stdout = (
            exception_stdout
            if exception_stdout is not None
            else process_stdout
        )
        stderr = (
            exception_stderr
            if exception_stderr is not None
            else process_stderr
        )

        logger.error(
            "ExifTool %s raised on %s: %s",
            operation,
            file_path,
            exception,
        )
        logger.error(
            "ExifTool failure diagnostics for %s — "
            "exit status: %r; stdout: %r; stderr: %r; "
            "exception type: %s; exception args: %r; "
            "CLI arguments: %r; tags: %r",
            file_path,
            status,
            stdout,
            stderr,
            type(exception).__name__,
            exception.args,
            cli_params,
            tags,
        )

    # ----- output inspection -----
    @staticmethod
    def _is_failure(result: Optional[str]) -> bool:
        # PyExifTool's execute() may return None when stdout is consumed
        # internally and no error occurred.
        if result is None:
            return False
        s = result.strip()
        if not s:
            return False

        lower = s.lower()

        # Hard failure strings exiftool emits on write errors.
        if "weren't updated" in lower or 'could not be' in lower:
            return True

        # "N image files updated" — failure only if N == 0.
        if 'updated' in lower:
            for token in lower.replace('\n', ' ').split():
                if token.isdigit():
                    return int(token) == 0

        # "N image files unchanged" (tags already matched) — not a failure.
        if 'unchanged' in lower:
            return False

        if 'error' in lower:
            return True

        return False

    # ----- parameter building -----
    def _build_params(self, file_path: str, metadata: Dict[str, Any],
                      previous: Dict[str, Any], is_video: bool) -> List[str]:
        """Build CLI-safe ExifTool arguments. Description is handled separately
        by _build_tag_dict() using a temporary UTF-8 file."""
        params: List[str] = []
        params.extend(self._keyword_params(file_path, metadata, previous, is_video))
        params.extend(self._favorite_params(metadata, previous, is_video))
        params.extend(self._location_params(metadata, previous))
        params.extend(self._datetime_params(metadata, previous, is_video))
        return params

    def _build_tag_dict(self, metadata: Dict[str, Any], previous: Dict[str, Any],
                        is_video: bool) -> Dict[str, Any]:
        """Build description tag/value pairs for file-based ExifTool writes."""
        cur = metadata.get('description', '') or ''
        prev = previous.get('description', '') or ''
        if cur == prev:
            return {}
        if is_video:
            return {'QuickTime:Description': cur}
        return {'EXIF:ImageDescription': cur, 'XMP:Description': cur}

    @staticmethod
    def _sanitize_arg_value(val: Any) -> str:
        """Flatten newlines/CRs so values cannot split into extra ExifTool args."""
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
        """Parse a date string into (exif_datetime, offset_or_None)."""
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

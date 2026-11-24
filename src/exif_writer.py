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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from exiftool import ExifToolHelper


logger = logging.getLogger(__name__)


PHOTO_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.tiff', '.tif',
    '.heic', '.heif', '.dng', '.cr2', '.nef', '.arw',
}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.m4v', '.3gp', '.webm'}


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

    # ----- lifecycle -----
    def _get_et(self) -> ExifToolHelper:
        if self._et is None:
            self._et = ExifToolHelper(common_args=['-m'])
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

    def write_metadata(self, file_path: str, metadata: Dict[str, Any],
                      previous_metadata: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_supported_file(file_path):
            logger.warning("Unsupported file type: %s", file_path)
            return False

        is_video_file = self.is_video(file_path)
        previous = previous_metadata or {}
        params = self._build_params(file_path, metadata, previous, is_video_file)

        if not params:
            logger.debug("No EXIF changes needed for %s", file_path)
            return True

        # Explicitly pass -overwrite_original only when backups are disabled.
        # Otherwise ExifTool's default is to save <file>_original.
        if not self._keep_backups:
            params = ['-overwrite_original'] + params
        params.append(file_path)

        if self._dry_run:
            logger.info("[DRY RUN] exiftool %s", params)
            return True

        try:
            logger.debug("ExifTool %s", params)
            result = self._get_et().execute(*params)
        except Exception as e:
            logger.error("ExifTool raised on %s: %s", file_path, e)
            return False

        if self._is_failure(result):
            logger.error("ExifTool reported failure for %s: %r", file_path, result)
            return False
        logger.debug("EXIF updated for %s", file_path)
        return True

    # ----- output inspection -----
    @staticmethod
    def _is_failure(result: Optional[str]) -> bool:
        # PyExifTool 0.5.6's execute() returns None when exiftool exits cleanly
        # (stdout is consumed internally and not surfaced). Treat None as success.
        if result is None:
            return False
        s = result.strip()
        if not s:
            return False  # empty string — also fine

        lower = s.lower()

        # Hard failure strings exiftool emits on write errors.
        if "weren't updated" in lower or 'could not be' in lower:
            return True

        # "N image files updated" — failure only if N == 0.
        m = re.search(r'(\d+)\s+(?:image\s+)?files?\s+updated', lower)
        if m:
            return int(m.group(1)) == 0

        # "N image files unchanged" (tags already matched) — not a failure.
        m = re.search(r'(\d+)\s+(?:image\s+)?files?\s+unchanged', lower)
        if m:
            return False

        if 'error' in lower:
            return True

        # Unknown output from a non-empty, non-error string — assume success
        # rather than mis-reporting every write as failed.
        return False

    # ----- parameter building -----
    def _build_params(self, file_path: str, metadata: Dict[str, Any],
                     previous: Dict[str, Any], is_video: bool) -> List[str]:
        params: List[str] = []
        params.extend(self._keyword_params(file_path, metadata, previous, is_video))
        params.extend(self._favorite_params(metadata, previous, is_video))
        params.extend(self._description_params(metadata, previous, is_video))
        params.extend(self._location_params(metadata, previous))
        params.extend(self._datetime_params(metadata, previous, is_video))
        return params

    def _keyword_params(self, file_path: str, metadata: Dict[str, Any],
                       previous: Dict[str, Any], is_video: bool) -> List[str]:
        current = list(metadata.get('albums') or [])
        prev = list(previous.get('albums') or [])

        if current == prev:
            return []  # fast path: no album change, don't touch the file

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
            if kw:
                params.append(f'-{tag}-={kw}')
        for kw in to_add:
            if kw:
                params.append(f'-{tag}+={kw}')
        return params

    def _read_keywords(self, file_path: str, tag: str) -> List[str]:
        # For XMP:Subject on video, exiftool may return the tag under the
        # fully-qualified group name XMP-dc:Subject depending on version.
        # Request both so we don't miss existing keywords.
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

        # PyExifTool may return keys with or without group prefix depending on
        # common_args. Try all candidate names and merge results.
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
    def _description_params(metadata: Dict[str, Any], previous: Dict[str, Any],
                           is_video: bool) -> List[str]:
        cur = metadata.get('description', '') or ''
        prev = previous.get('description', '') or ''
        if cur == prev:
            return []
        if is_video:
            return [f'-QuickTime:Description={cur}'
                    if cur else '-QuickTime:Description=']
        if cur:
            return [f'-EXIF:ImageDescription={cur}', f'-XMP:Description={cur}']
        return ['-EXIF:ImageDescription=', '-XMP:Description=']

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
        """Parse a date string into (exif_datetime, offset_or_None).

        exif_datetime is 'YYYY:MM:DD HH:MM:SS' (EXIF standard).
        offset_or_None is '+HH:MM' / '-HH:MM' when the input carried a UTC
        offset, or None when the input was naive (no zone info). The offset
        should be written to OffsetTimeOriginal / OffsetTime so that the
        wall-clock time and the zone are not silently separated.

        Returns ('', None) when the input cannot be parsed — callers must
        treat this as "skip the write" to avoid storing garbage.
        """
        if not raw:
            return '', None
        s = str(raw).strip()

        # Already in EXIF format with no zone — keep as-is, no offset.
        try:
            datetime.strptime(s, '%Y:%m:%d %H:%M:%S')
            return s, None
        except ValueError:
            pass

        # ISO 8601 (with or without offset / Z).
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
            # Had a value but couldn't parse it — refuse to write garbage.
            return []

        if is_video:
            if cur:
                params = [f'-QuickTime:CreateDate={cur}',
                          f'-QuickTime:ModifyDate={cur}']
                # QuickTime stores timezone as a UTC offset in a separate atom.
                if offset:
                    params.append(f'-QuickTime:OffsetTime={offset}')
                return params
            return ['-QuickTime:CreateDate=', '-QuickTime:ModifyDate=',
                    '-QuickTime:OffsetTime=']

        if cur:
            params = [f'-EXIF:DateTimeOriginal={cur}', f'-EXIF:CreateDate={cur}']
            if offset:
                # OffsetTimeOriginal pairs with DateTimeOriginal; OffsetTime
                # pairs with ModifyDate/CreateDate.
                params += [f'-EXIF:OffsetTimeOriginal={offset}',
                           f'-EXIF:OffsetTime={offset}']
            return params
        return ['-EXIF:DateTimeOriginal=', '-EXIF:CreateDate=',
                '-EXIF:OffsetTimeOriginal=', '-EXIF:OffsetTime=']

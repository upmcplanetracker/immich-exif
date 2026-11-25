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


# Extensions where exiftool supports WRITING metadata (R/W in the format table
# at https://exiftool.org/index.html), filtered to formats Immich ingests.
# Source: exiftool 13.64 format table, cross-referenced with Immich's supported
# media list. Read-only formats (3FR, DCR, KDC, etc.) are intentionally excluded
# — exiftool would open them but refuse to write, producing silent no-ops.
#
# Photo / image formats:
#   JPEG/JPG/JPE    — baseline; always writable
#   PNG             — writable (XMP, EXIF, IPTC via non-standard chunks)
#   GIF             — writable (XMP, XMP:dc)
#   WEBP            — writable (XMP, EXIF, ICC)
#   TIFF/TIF        — writable (full EXIF/IPTC/XMP)
#   HEIC/HEIF/HIF   — writable (XMP, QuickTime atoms)
#   AVIF            — writable (XMP, QuickTime atoms)
#   JXL             — writable (EXIF, XMP) — JPEG XL, ISO BMFF and codestream
#   DNG             — writable (full EXIF/IPTC/XMP) — Digital Negative
#   CR2             — writable (Canon RAW 2, TIFF-based)
#   CR3             — writable (Canon RAW 3, QuickTime-based)
#   NEF/NRW         — writable (Nikon RAW, TIFF-based)
#   ARW/SR2         — writable (Sony RAW, TIFF-based)
#   RW2             — writable (Panasonic RAW 2)
#   ORF/ORI         — writable (Olympus RAW, TIFF-based)
#   RAF             — writable (FujiFilm RAW)
#   PEF             — writable (Pentax RAW, TIFF-based)
#   SRW             — writable (Samsung RAW, TIFF-based)
#   X3F             — writable (Sigma/Foveon RAW)
#   ERF             — writable (Epson RAW, TIFF-based)
#   MEF             — writable (Mamiya RAW, TIFF-based)
#   MOS             — writable (Creo Leaf Mosaic, TIFF-based)
#   MRW             — writable (Minolta RAW)
#   RWL             — writable (Leica RAW, Panasonic-based)
#   IIQ             — writable (Phase One RAW, TIFF-based)
#   GPR             — writable (GoPro RAW, DNG-based)
#
# Video formats (metadata written via QuickTime/XMP atoms):
#   MP4             — writable
#   MOV/QT          — writable
#   M4V             — writable (MPEG-4 Video, QuickTime-based)
#   M4A             — writable (MPEG-4 Audio, QuickTime-based)
#   3GP/3GPP        — writable
#   3G2/3GP2        — writable
#   MKV             — read-only in exiftool (Matroska); included so the
#                     tool logs a sensible skip rather than "unsupported"
#   WEBM            — read-only in exiftool (Matroska); same rationale
#   AVI             — read-only (RIFF); same rationale
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

# Videos: exiftool writes QuickTime/XMP atoms for the first group.
# MKV/WEBM/AVI are read-only in exiftool but Immich stores them, so we
# include them here and let is_supported_file() pass them through —
# exiftool will simply make no changes and report "1 image files unchanged",
# which _is_failure() correctly treats as a non-failure.
VIDEO_EXTENSIONS = {
    '.mp4',
    '.mov', '.qt',
    '.m4v', '.m4a',
    '.3gp', '.3gpp',
    '.3g2', '.3gp2',
    '.mkv',                        # Matroska — exiftool read-only
    '.webm',                       # Matroska/VP9 — exiftool read-only
    '.avi',                        # RIFF — exiftool read-only
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

        # Split into two write passes:
        #   1. params  — scalar/simple tags via execute() (keywords, GPS, dates,
        #                favorites). Safe as CLI args.
        #   2. tag_dict — description, written via set_tags() which sends values
        #                 through the exiftool JSON pipe and is immune to newlines,
        #                 special characters, and AI caption markers like [/AI].
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
                logger.error("ExifTool execute raised on %s: %s", file_path, e)
                ok = False
            else:
                if self._is_failure(result):
                    logger.error(
                        "ExifTool reported failure for %s — stdout: %r  stderr: %r",
                        file_path,
                        result if isinstance(result, str) else None,
                        getattr(self._get_et(), '_last_stderr', None))
                    ok = False

        # Pass 2: description via set_tags() — values go through JSON pipe,
        # so newlines and special characters are never parsed as CLI args.
        if tag_dict and ok:
            extra: List[str] = []
            if not self._keep_backups:
                extra = ['-overwrite_original']
            try:
                logger.debug("ExifTool set_tags %s on %s", tag_dict, file_path)
                self._get_et().set_tags(
                    file_path, tags=tag_dict, params=extra or None)
            except Exception as e:
                logger.error("ExifTool set_tags raised on %s: %s", file_path, e)
                ok = False

        if ok:
            logger.debug("EXIF updated for %s", file_path)
        return ok

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
        """Build CLI-safe exiftool arguments. Description is excluded — it goes
        through set_tags() via _build_tag_dict() to handle newlines safely."""
        params: List[str] = []
        params.extend(self._keyword_params(file_path, metadata, previous, is_video))
        params.extend(self._favorite_params(metadata, previous, is_video))
        params.extend(self._location_params(metadata, previous))
        params.extend(self._datetime_params(metadata, previous, is_video))
        return params

    def _build_tag_dict(self, metadata: Dict[str, Any], previous: Dict[str, Any],
                        is_video: bool) -> Dict[str, Any]:
        """Build a tag dict for set_tags() — safe for any string value.
        Currently handles description only, since all other tags are scalar/enum
        values that are safe as CLI args."""
        cur = metadata.get('description', '') or ''
        prev = previous.get('description', '') or ''
        if cur == prev:
            return {}
        if is_video:
            return {'QuickTime:Description': cur} if cur else {'QuickTime:Description': ''}
        if cur:
            return {'EXIF:ImageDescription': cur, 'XMP:Description': cur}
        return {'EXIF:ImageDescription': '', 'XMP:Description': ''}

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

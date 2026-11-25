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

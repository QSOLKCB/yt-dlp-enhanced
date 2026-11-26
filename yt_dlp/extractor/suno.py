import json
import math
import re
from html.parser import HTMLParser

from .common import InfoExtractor
from ..utils import ExtractorError, parse_iso8601, url_or_none
from ..utils.traversal import traverse_obj


def _strict_json(data):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON member')
            result[key] = value
        return result

    def finite_number(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Non-finite JSON number')
        return number

    return json.loads(data, object_pairs_hook=unique_pairs,
                      parse_float=finite_number, parse_constant=finite_number)


class _FlightScripts(HTMLParser):
    # Treat raw-text and RCDATA elements as text rather than nested markup.
    CDATA_CONTENT_ELEMENTS = (*HTMLParser.CDATA_CONTENT_ELEMENTS,
                              'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript')
    MAX_PAGE_BYTES = 8 * 1024 * 1024
    MAX_FLIGHT_BYTES = 4 * 1024 * 1024
    MAX_SCRIPTS = 4096
    _PUSH = re.compile(r'(?:self\s*\.\s*__next_f|\(\s*self\.__next_f\s*=\s*self\.__next_f\s*\|\|\s*\[\]\s*\))\s*\.\s*push\s*\(')

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.segments = []
        self._size = 0
        self._count = 0
        self._templates = 0
        self._plaintext = False
        self._script = None

    def handle_starttag(self, tag, attrs):
        if self._plaintext:
            return
        if tag == 'plaintext':
            self._plaintext = True
        elif tag == 'template':
            self._templates += 1
        elif tag == 'script':
            self._count += 1
            if self._count > self.MAX_SCRIPTS:
                raise ExtractorError('Too many Suno Flight scripts', expected=True)
            attrs = dict(attrs)
            script_type = (attrs.get('type') or '').strip().lower()
            if (not self._templates and 'src' not in attrs
                    and script_type in ('', 'module', 'text/javascript', 'application/javascript',
                                        'text/ecmascript', 'application/ecmascript')):
                self._script = []

    def handle_startendtag(self, tag, attrs):
        # A slash does not close non-void HTML raw-text elements or templates.
        if tag in (*self.CDATA_CONTENT_ELEMENTS, 'template', 'plaintext'):
            self.handle_starttag(tag, attrs)
            if tag in self.CDATA_CONTENT_ELEMENTS:
                self.set_cdata_mode(tag)

    def handle_data(self, data):
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == 'template' and self._templates:
            self._templates -= 1
        if tag != 'script' or self._script is None:
            return
        script = ''.join(self._script).strip()
        self._script = None
        if '__next_f' not in script:
            return
        match = self._PUSH.match(script)
        script = script.removesuffix(';').rstrip()
        if not match or not script.endswith(')'):
            raise ExtractorError('Invalid Suno Flight segment', expected=True)
        self._size += len(script.encode('utf-8'))
        if self._size > self.MAX_FLIGHT_BYTES:
            raise ExtractorError('Suno Flight data exceeds size limit', expected=True)
        self.segments.append(script[match.end():-1])

    @classmethod
    def extract(cls, webpage):
        parser = cls()
        try:
            if len(webpage) > cls.MAX_PAGE_BYTES or len(webpage.encode('utf-8')) > cls.MAX_PAGE_BYTES:
                raise ExtractorError('Suno webpage exceeds size limit', expected=True)
            parser.feed(webpage)
            parser.close()
        except (ValueError, RecursionError) as e:
            raise ExtractorError('Invalid Suno HTML or Flight encoding', expected=True) from e
        if parser._script is not None:
            raise ExtractorError('Invalid Suno Flight segment: unclosed script', expected=True)
        return parser.segments


class SunoIE(InfoExtractor):
    IE_DESC = 'Suno (metadata only)'
    _UUID = r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
    _VALID_URL = rf'(?i:https?://(?:www\.)?suno\.com)/(?:song/(?P<id>{_UUID})|s/(?P<share_id>[A-Za-z0-9_-]+))/?(?=[?#]|$)'
    _TESTS = [{
        'url': 'https://suno.com/song/01234567-89ab-4cde-8f01-23456789abcd',
        'only_matching': True,
    }, {
        'url': 'https://suno.com/s/ExampleShareToken',
        'only_matching': True,
    }]

    @staticmethod
    def _flight_models(flight):
        # Flight text/binary rows are byte-length framed; model rows end in LF.
        # Do not resynchronise after a malformed frame or inspect tagged payloads.
        try:
            data = flight.encode('utf-8')
        except UnicodeError as e:
            raise ExtractorError('Invalid Suno Flight encoding', expected=True) from e
        # Resource hints alone have no row ID: :H<code><JSON>\n
        row_header = re.compile(rb'(?:[0-9a-f]+:|:(?=H[A-Za-z]))')
        length_header = re.compile(rb'[TAOobUSsLlGgMmV]([0-9a-f]{1,16}),')
        pos = 0
        while pos < len(data):
            header = row_header.match(data, pos)
            if not header or header.end() == len(data):
                raise ExtractorError('Invalid Suno Flight row', expected=True)
            pos = header.end()
            if data[pos] in b'TAOobUSsLlGgMmV':
                length = length_header.match(data, pos)
                if not length:
                    raise ExtractorError('Invalid Suno Flight row length', expected=True)
                pos = length.end() + int(length.group(1), 16)
                if pos > len(data):
                    raise ExtractorError('Truncated Suno Flight row', expected=True)
                continue

            end = data.find(b'\n', pos)
            if end < 0:
                raise ExtractorError('Truncated Suno Flight row', expected=True)
            row = data[pos:end]
            pos = end + 1
            if header.group() == b':':
                # Validate the hint payload, but never treat it as a model.
                try:
                    _strict_json(row[2:].decode('utf-8'))
                except (ValueError, RecursionError) as e:
                    raise ExtractorError('Invalid Suno Flight hint', expected=True) from e
                continue
            if row and row[0] in b'ABCDEFGHIJKLMNOPQRSTUVWXYZ#rx':
                continue
            try:
                yield _strict_json(row.decode('utf-8'))
            except (ValueError, RecursionError) as e:
                raise ExtractorError('Invalid Suno Flight model', expected=True) from e

    @staticmethod
    def _prefetched_objects(model):
        # Traverse decoded containers only, never JSON-looking string values.
        pending = [model]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if isinstance(value.get('prefetched'), dict):
                    yield value['prefetched']
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)

    def _public_clip(self, webpage, video_id):
        # The prefetched query can occur after multiline React Flight text rows.
        # Decode the JSON strings without interpreting JavaScript or fetching media.
        chunks = []
        for segment in _FlightScripts.extract(webpage):
            try:
                payload = _strict_json(segment)
            except (ValueError, RecursionError) as e:
                raise ExtractorError('Invalid Suno Flight segment', expected=True) from e
            if not isinstance(payload, list) or not payload or type(payload[0]) is not int:
                raise ExtractorError('Invalid Suno Flight segment', expected=True)
            segment_type = payload[0]
            if segment_type == 1 and len(payload) == 2 and isinstance(payload[1], str):
                chunks.append(payload[1])
            elif (segment_type == 0 and len(payload) == 1) or (segment_type == 2 and len(payload) == 2):
                # Bootstrap and form state are not Flight model data.
                continue
            elif segment_type == 3:
                raise ExtractorError('Unsupported Suno Flight binary segment', expected=True)
            else:
                raise ExtractorError('Invalid or unsupported Suno Flight segment', expected=True)

        clip = None
        clip_identity = None
        for model in self._flight_models(''.join(chunks)):
            for prefetched in self._prefetched_objects(model):
                for query in traverse_obj(prefetched, ('dehydratedState', 'queries', ..., {dict})):
                    query_key = query.get('queryKey')
                    if not isinstance(query_key, list) or not query_key or query_key[0] != 'clips':
                        continue
                    candidate = traverse_obj(query, ('state', 'data', {dict}))
                    candidate_id = candidate.get('id') if candidate else None
                    key_id = query_key[1] if len(query_key) == 2 else None
                    key_id = key_id.lower() if isinstance(key_id, str) and re.fullmatch(self._UUID, key_id) else None
                    candidate_id = candidate_id.lower() if isinstance(candidate_id, str) and re.fullmatch(self._UUID, candidate_id) else None
                    # An unrelated query can be ignored, but a contradiction involving
                    # the requested song cannot be discarded in favour of a later record.
                    if video_id not in (key_id, candidate_id):
                        continue
                    if key_id != video_id or candidate_id != video_id:
                        raise ExtractorError('Inconsistent Suno query identity', expected=True)
                    if type(candidate.get('is_public')) is not bool:
                        raise ExtractorError('Invalid Suno visibility flag', expected=True)
                    candidate = {**candidate, 'id': video_id}
                    # JSON representation preserves boolean/number type distinctions.
                    identity = json.dumps(candidate, sort_keys=True)
                    if clip_identity is not None and clip_identity != identity:
                        raise ExtractorError('Conflicting Suno song metadata', expected=True)
                    clip, clip_identity = candidate, identity
        if not clip or clip.get('is_public') is not True or clip.get('status') != 'complete':
            raise ExtractorError('Public Suno song metadata unavailable', expected=True)
        return clip

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        webpage, handle = self._download_webpage_handle(
            'https://' + url.split('://', 1)[1], match.group('id') or match.group('share_id'))
        final_match = self._match_valid_url(handle.url)
        if not final_match or not final_match.group('id') or not handle.url.lower().startswith('https://'):
            raise ExtractorError('Suno did not return a canonical song URL', expected=True)
        video_id = final_match.group('id').lower()
        if match.group('id') and match.group('id').lower() != video_id:
            raise ExtractorError('Suno returned a different song', expected=True)

        clip = self._public_clip(webpage, video_id)
        info = traverse_obj(clip, {
            'title': ('title', {str}),
            'uploader': ('display_name', {str}),
            'uploader_id': ('handle', {str}),
            'timestamp': ('created_at', {parse_iso8601}),
            'thumbnail': ('image_url', {url_or_none}),
        })
        duration = traverse_obj(clip, ('metadata', 'duration'))
        # Conservative one-day metadata ceiling, not a provider duration guarantee.
        if type(duration) in (int, float) and 0 < duration <= 86400:
            info['duration'] = duration

        title = info.get('title') or self._og_search_title(webpage)
        if not title:
            raise ExtractorError('Suno song title unavailable', expected=True)
        # Visibility is declared by the matching song object. No documented
        # per-song rights or download-permission interface has been established.
        # Playback URLs, ownership claims and provenance are not download grants.
        capabilities = {
            'visibility': 'public',
            'drm': 'unknown',
            'copyright_status': 'unknown',
            'license': 'unknown',
            'download_permission': 'unknown',
            'playback_permission': 'unknown',
            'authentication_required': 'unknown',
            'provenance': 'unknown',
        }
        return {
            **info,
            'id': video_id,
            'title': title,
            'description': self._og_search_description(webpage),
            'thumbnail': info.get('thumbnail') or url_or_none(self._og_search_thumbnail(webpage)),
            'formats': [],
            'resource_capabilities': capabilities,
        }

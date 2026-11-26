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
        self._script_phase = None
        self._flight_phase = None

    @staticmethod
    def _script_type(attrs):
        # HTML retains the first occurrence of a duplicate attribute.
        # A parameterized type is a data block, not a classic script.
        script_type = attrs.get('type')
        if script_type is None:
            language = attrs.get('language', '')
            script_type = f'text/{language}' if language else 'text/javascript'
        elif not script_type:
            script_type = 'text/javascript'
        else:
            if script_type.lower() == 'module':
                return 'module'
            # Only classic MIME matching trims HTML whitespace. A blank but
            # nonempty type is not the default, and language is not trimmed.
            script_type = script_type.strip(' \t\n\r\f')
        if script_type.lower() in (
                'application/ecmascript', 'application/javascript',
                'application/x-ecmascript', 'application/x-javascript',
                'text/ecmascript', 'text/javascript', 'text/javascript1.0',
                'text/javascript1.1', 'text/javascript1.2', 'text/javascript1.3',
                'text/javascript1.4', 'text/javascript1.5', 'text/jscript',
                'text/livescript', 'text/x-ecmascript', 'text/x-javascript'):
            # Legacy event-handler scripts are inert unless both attributes
            # identify the window load event. Modules do not use this gate.
            if 'for' in attrs and 'event' in attrs:
                target = attrs['for'].strip(' \t\n\r\f').lower()
                event = attrs['event'].strip(' \t\n\r\f').lower()
                if target != 'window' or event not in ('onload', 'onload()'):
                    return None
            return 'classic'
        return None

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
            attrs = {key: value or '' for key, value in reversed(attrs)}
            if self._templates or 'src' in attrs:
                return
            script_type = self._script_type(attrs)
            # HTMLParser may discard numeric references to U+000B while decoding
            # attributes. Conservatively check the original start tag as well.
            if (any('\v' in attrs.get(key, '') for key in ('type', 'language', 'for', 'event'))
                    or re.search(r'&#(?:0*11(?![0-9])|[xX]0*[bB](?![0-9a-fA-F]))', self.get_starttag_text())):
                # Browser control-character handling differs from HTML whitespace.
                # Inspect the body only to reject ambiguous Flight declarations.
                self._script_phase = 'ambiguous'
            elif script_type == 'module' and 'async' in attrs:
                self._script_phase = 'async'
            elif script_type and (script_type == 'module' or 'nomodule' not in attrs):
                self._script_phase = script_type
            else:
                return
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
        if self._script_phase in ('ambiguous', 'async'):
            raise ExtractorError('Unsupported Suno Flight script authority or execution phase', expected=True)
        # Classic and deferred module pushes do not necessarily execute in
        # document order. Never join a mixed stream or guess its scheduling.
        if self._flight_phase is not None and self._flight_phase != self._script_phase:
            raise ExtractorError('Mixed Suno Flight execution phases', expected=True)
        self._flight_phase = self._script_phase
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
                    candidate = traverse_obj(query, ('state', 'data', {dict}))
                    candidate_id = candidate.get('id') if candidate else None
                    clip_key = isinstance(query_key, list) and query_key and query_key[0] == 'clips'
                    key_id = query_key[1] if clip_key and len(query_key) >= 2 else None
                    key_id = key_id.lower() if isinstance(key_id, str) and re.fullmatch(self._UUID, key_id) else None
                    candidate_id = candidate_id.lower() if isinstance(candidate_id, str) and re.fullmatch(self._UUID, candidate_id) else None
                    # An unrelated query can be ignored, but a contradiction involving
                    # the requested song cannot be discarded in favour of a later record.
                    if video_id not in (key_id, candidate_id):
                        continue
                    if not clip_key or len(query_key) != 2 or key_id != video_id or candidate_id != video_id:
                        raise ExtractorError('Inconsistent Suno query identity', expected=True)
                    if type(candidate.get('is_public')) is not bool:
                        raise ExtractorError('Invalid Suno visibility flag', expected=True)
                    candidate = {**candidate, 'id': video_id}
                    # JSON representation preserves boolean/number type distinctions.
                    try:
                        identity = json.dumps(candidate, sort_keys=True)
                    except (ValueError, RecursionError) as e:
                        raise ExtractorError('Invalid Suno song metadata', expected=True) from e
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

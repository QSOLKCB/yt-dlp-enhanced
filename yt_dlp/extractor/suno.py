import json
import math
import re

from .common import InfoExtractor
from ..utils import ExtractorError, parse_iso8601, url_or_none
from ..utils.traversal import traverse_obj


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
                    json.loads(row[2:].decode('utf-8'))
                except (ValueError, RecursionError) as e:
                    raise ExtractorError('Invalid Suno Flight hint', expected=True) from e
                continue
            if row and row[0] in b'ABCDEFGHIJKLMNOPQRSTUVWXYZ#rx':
                continue
            try:
                yield json.loads(row.decode('utf-8'))
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
        for segment in re.findall(r'<script\b[^>]*>self\.__next_f\.push\((.*?)</script>', webpage, flags=re.DOTALL):
            if not segment.endswith(')'):
                raise ExtractorError('Invalid Suno Flight segment', expected=True)
            try:
                payload = json.loads(segment[:-1])
            except (ValueError, RecursionError) as e:
                raise ExtractorError('Invalid Suno Flight segment', expected=True) from e
            if (isinstance(payload, list) and len(payload) == 2
                    and type(payload[0]) is int and payload[0] == 1 and isinstance(payload[1], str)):
                chunks.append(payload[1])

        clip = None
        for model in self._flight_models(''.join(chunks)):
            for prefetched in self._prefetched_objects(model):
                for query in traverse_obj(prefetched, ('dehydratedState', 'queries', ..., {dict})):
                    query_key = query.get('queryKey')
                    if not isinstance(query_key, list) or not query_key or query_key[0] != 'clips':
                        continue
                    candidate = traverse_obj(query, ('state', 'data', {dict}))
                    candidate_id = candidate.get('id') if candidate else None
                    if not isinstance(candidate_id, str) or not re.fullmatch(self._UUID, candidate_id):
                        continue
                    if candidate_id.lower() != video_id:
                        continue
                    candidate = {**candidate, 'id': video_id}
                    if clip is not None and clip != candidate:
                        raise ExtractorError('Conflicting Suno song metadata', expected=True)
                    clip = candidate
        if not clip or clip.get('is_public') is not True or clip.get('status') != 'complete':
            raise ExtractorError('Public Suno song metadata unavailable', expected=True)
        return clip

    def _real_extract(self, url):
        match = self._match_valid_url(url)
        webpage, handle = self._download_webpage_handle(
            'https://' + url.split('://', 1)[1], match.group('id') or match.group('share_id'))
        final_match = self._match_valid_url(handle.url)
        if not final_match or not final_match.group('id'):
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
        if type(duration) in (int, float) and 0 < duration < math.inf:
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

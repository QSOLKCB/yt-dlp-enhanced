import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from yt_dlp import YoutubeDL
from yt_dlp.extractor import SunoIE
from yt_dlp.utils import ExtractorError


class TestSuno(unittest.TestCase):
    _ID = '01234567-89ab-4cde-8f01-23456789abcd'
    _URL = f'https://suno.com/song/{_ID}'
    _SHARE = 'https://suno.com/s/ExampleShareToken'

    def setUp(self):
        self.ydl = YoutubeDL({
            'quiet': True,
            'no_warnings': True,
            'ignore_no_formats_error': True,
            'format': 'best',
        }, auto_init=False)
        self.addCleanup(self.ydl.close)
        self.ie = SunoIE(self.ydl)
        self.clip = {
            'id': self._ID,
            'title': 'Synthetic song',
            'display_name': 'Synthetic artist',
            'handle': 'synthetic_artist',
            'metadata': {'duration': 231.6},
            'created_at': '2026-11-22T17:40:49Z',
            'image_url': 'https://example.invalid/cover.jpg',
            'is_public': True,
            'status': 'complete',
            'audio_url': 'https://example.invalid/api/forbidden',
            'media_urls': {'playback': 'https://example.invalid/playback.m4a'},
        }

    @staticmethod
    def _model(*clips):
        queries = [{'queryKey': ['clips', c['id']], 'state': {'data': c}} for c in clips]
        return ['$', '$L1', None, {'prefetched': {'dehydratedState': {'queries': queries}}}]

    @staticmethod
    def _text_row(text):
        return f'a:T{len(text.encode("utf-8")):x},{text}'

    def _page(self, *clips):
        flight = self._text_row('hello\nworld') + 'b:' + json.dumps(self._model(*clips)) + '\n'
        # Split the embedded object between two Flight script segments.
        return self._flight_page(flight, split=flight.index('dehydratedState') + 5)

    @staticmethod
    def _flight_page(flight, *, split=None):
        pos = len(flight) // 2 if split is None else split
        return (
            '<meta property="og:title" content="Fallback title">'
            '<meta property="og:description" content="Synthetic description">'
            '<meta property="og:image" content="https://example.invalid/fallback.jpg">'
            '<audio src="https://example.invalid/sil-100.mp3"></audio>'
            '<audio src="https://example.invalid/api/forbidden"></audio>'
            + ''.join(f'<script>self.__next_f.push({json.dumps([1, chunk])})</script>'
                      for chunk in (flight[:pos], flight[pos:])))

    def _extract(self, webpage, url=None, final_url=None, *, expected_request=None):
        with patch.object(self.ie, '_download_webpage_handle', return_value=(
                webpage, SimpleNamespace(url=final_url or self._URL))) as download:
            try:
                return self.ie._real_extract(url or self._URL)
            finally:
                download.assert_called_once_with(*(expected_request or (self._URL, self._ID)))

    def test_url_matching(self):
        for url in (self._URL, self._SHARE, self._URL + '?sh=example', self._URL + '/',
                    'HTTPS://SUNO.COM/song/' + self._ID, 'HtTp://WWW.SuNo.CoM/s/ExampleShareToken'):
            self.assertTrue(SunoIE.suitable(url))
        for url in ('https://suno.com.evil.invalid/song/' + self._ID,
                    self._URL + 'x', self._URL + '/extra', 'https://suno.com/song/not-a-uuid',
                    'https://suno.com/create', 'https://SUNO.COM/SONG/' + self._ID):
            self.assertFalse(SunoIE.suitable(url))

    def test_mixed_case_urls(self):
        for url, request_url, request_id in (
                ('HTTPS://SUNO.COM/song/' + self._ID, 'https://SUNO.COM/song/' + self._ID, self._ID),
                ('HtTp://WWW.SuNo.CoM/s/ExampleShareToken', 'https://WWW.SuNo.CoM/s/ExampleShareToken', 'ExampleShareToken'),
        ):
            with self.subTest(url=url):
                result = self._extract(
                    self._page(self.clip), url, 'HTTPS://SUNO.COM/song/' + self._ID.upper(),
                    expected_request=(request_url, request_id))
                self.assertEqual(result['id'], self._ID)

    def test_uppercase_clip_id(self):
        uppercase_clip = dict(self.clip, id=self._ID.upper())
        for clips in ((uppercase_clip,), (self.clip, uppercase_clip)):
            with self.subTest(clips=clips):
                result = self._extract(self._page(*clips))
                self.assertEqual(result['id'], self._ID)
        with self.assertRaisesRegex(ExtractorError, 'Conflicting'):
            self._extract(self._page(self.clip, dict(uppercase_clip, title='Conflicting title')))

    def test_metadata_and_no_media(self):
        for url, request_id in ((self._URL, self._ID), (self._SHARE, 'ExampleShareToken')):
            with self.subTest(url=url):
                result = self._extract(
                    self._page(self.clip), url, self._URL + '?sh=example', expected_request=(url, request_id))
                self.assertEqual(result['id'], self._ID)
                self.assertEqual(result['title'], 'Synthetic song')
                self.assertEqual(result['uploader'], 'Synthetic artist')
                self.assertEqual(result['uploader_id'], 'synthetic_artist')
                self.assertEqual(result['duration'], 231.6)
                self.assertEqual(result['timestamp'], 1795369249)
                self.assertEqual(result['thumbnail'], 'https://example.invalid/cover.jpg')
                self.assertEqual(result['formats'], [])
                self.assertNotIn('url', result)
                self.assertNotIn('media_urls', result)
                self.assertEqual(result['resource_capabilities'], {
                    'visibility': 'public',
                    'drm': 'unknown',
                    'copyright_status': 'unknown',
                    'license': 'unknown',
                    'download_permission': 'unknown',
                    'playback_permission': 'unknown',
                    'authentication_required': 'unknown',
                    'provenance': 'unknown',
                })

    def test_undocumented_capability_flags_are_not_authority(self):
        # These are hypothetical input fields, not a supported Suno contract.
        for flags in (
                {'has_drm': False, 'copyright_status': 'public_domain', 'license': 'CC0', 'download_allowed': True},
                {'has_drm': True, 'copyright_status': 'copyrighted', 'license': 'CC-BY-4.0', 'download_allowed': False},
                {'is_owned': True, 'provenance': 'verified_suno', 'action_config': {'playback': {'enabled': True}}},
                {'download_allowed': 'true', 'resource_capabilities': {'download_permission': 'allowed'}},
        ):
            with self.subTest(flags=flags):
                result = self._extract(self._page(dict(self.clip, **flags)))
                self.assertEqual(result['resource_capabilities']['visibility'], 'public')
                self.assertTrue(all(value == 'unknown' for key, value in result['resource_capabilities'].items()
                                    if key != 'visibility'))
                self.assertEqual(result['formats'], [])
                self.assertNotIn('url', result)

    def test_fallback_metadata(self):
        clip = dict(self.clip, title=None, image_url=None, metadata={'duration': True})
        result = self._extract(self._page(clip))
        self.assertEqual(result['title'], 'Fallback title')
        self.assertEqual(result['thumbnail'], 'https://example.invalid/fallback.jpg')
        self.assertNotIn('duration', result)

    def _process(self, result):
        return self.ydl.process_video_result({
            **result,
            'extractor': self.ie.IE_NAME,
            'extractor_key': self.ie.ie_key(),
            'webpage_url': self._URL,
        }, download=False)

    def test_no_formats_default_error(self):
        self.ydl.params['ignore_no_formats_error'] = False
        result = self._extract(self._page(self.clip))
        with self.assertRaisesRegex(ExtractorError, 'No video formats found'):
            self._process(result)

    def test_no_formats_warning_once(self):
        with patch.object(self.ydl, 'report_warning') as warning:
            result = self._extract(self._page(self.clip))
            warning.assert_not_called()
            processed = self._process(result)
        self.assertEqual(processed['formats'], [])
        self.assertEqual(processed['resource_capabilities'], result['resource_capabilities'])
        self.assertEqual(sum(call.args[0] == 'No video formats found!' for call in warning.call_args_list), 1)

    def test_unavailable_or_wrong_song(self):
        for page in ('<html></html>', self._page(dict(self.clip, is_public=False)),
                     self._page(dict(self.clip, is_public=1)), self._page(dict(self.clip, status='pending')),
                     self._page(dict(self.clip, id='different')), self._page(dict(self.clip, id=123))):
            with self.subTest(page=page), self.assertRaises(ExtractorError) as error:
                self._extract(page)
            self.assertTrue(error.exception.expected)
        with self.assertRaisesRegex(ExtractorError, 'Conflicting'):
            self._extract(self._page(self.clip, dict(self.clip, title='Conflicting title')))

    def test_query_shaped_text_row_is_not_metadata(self):
        fake = dict(self.clip, title='False song')
        # Include a full fake row inside text, as well as non-ASCII UTF-8.
        text = 'lyric é 🎵\n' + 'c:' + json.dumps(self._model(fake)) + '\n'
        flight = self._text_row(text)
        with self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
            self._extract(self._flight_page(flight))
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        # Split the encoded text row across script segments, not just the model.
        result = self._extract(self._flight_page(flight + genuine, split=flight.index('🎵') + 1))
        self.assertEqual(result['title'], self.clip['title'])

    def test_query_shaped_model_string_is_not_metadata(self):
        text = json.dumps(self._model(self.clip))
        flight = 'a:' + json.dumps({'description': text}) + '\n'
        with self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
            self._extract(self._flight_page(flight))

    def test_tagged_rows_are_not_models(self):
        fake = json.dumps(self._model(dict(self.clip, title='False song')))
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        for row in ('a:D' + fake + '\n', f'a:A{len(fake.encode("utf-8")):x},{fake}'):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
                    self._extract(self._flight_page(row))
                result = self._extract(self._flight_page(row + genuine))
                self.assertEqual(result['title'], self.clip['title'])

    def test_malformed_flight_rows_fail_closed(self):
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        for bad in ('a:Tx,hello', 'a:Tffff,hello', 'a:T1,é', 'garbage\n', 'a:{\n'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ExtractorError, 'Flight'):
                self._extract(self._flight_page(bad + genuine))
        with self.assertRaisesRegex(ExtractorError, 'Truncated'):
            self._extract(self._flight_page(genuine.rstrip('\n')))

    def test_invalid_redirect(self):
        for final_url in ('https://example.invalid/song/' + self._ID, self._SHARE,
                          'https://suno.com/song/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'):
            with self.subTest(final_url=final_url), self.assertRaises(ExtractorError):
                self._extract(self._page(self.clip), final_url=final_url)

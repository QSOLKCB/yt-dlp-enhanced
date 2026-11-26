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
        self.ydl = YoutubeDL({'quiet': True, 'no_warnings': True, 'ignore_no_formats_error': True}, auto_init=False)
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

    def _page(self, *clips):
        queries = [{'queryKey': ['clips', c['id']], 'state': {'data': c}} for c in clips]
        flight = 'a:T5,hello\nworld\nb:' + json.dumps({
            'prefetched': {'dehydratedState': {'queries': queries}},
        })
        # Split the embedded object between two Flight script segments.
        pos = flight.index('dehydratedState') + 5
        return (
            '<meta property="og:title" content="Fallback title">'
            '<meta property="og:description" content="Synthetic description">'
            '<meta property="og:image" content="https://example.invalid/fallback.jpg">'
            '<audio src="https://example.invalid/sil-100.mp3"></audio>'
            '<audio src="https://example.invalid/api/forbidden"></audio>'
            + ''.join(f'<script>self.__next_f.push({json.dumps([1, chunk])})</script>'
                      for chunk in (flight[:pos], flight[pos:])))

    def _extract(self, webpage, url=None, final_url=None):
        with patch.object(self.ie, '_download_webpage_handle', return_value=(
                webpage, SimpleNamespace(url=final_url or self._URL))) as download:
            result = self.ie._real_extract(url or self._URL)
            download.assert_called_once()
            return result

    def test_url_matching(self):
        for url in (self._URL, self._SHARE, self._URL + '?sh=example', self._URL + '/'):
            self.assertTrue(SunoIE.suitable(url))
        for url in ('https://suno.com.evil.invalid/song/' + self._ID,
                    self._URL + 'x', self._URL + '/extra', 'https://suno.com/song/not-a-uuid',
                    'https://suno.com/create'):
            self.assertFalse(SunoIE.suitable(url))

    def test_metadata_and_no_media(self):
        for url in (self._URL, self._SHARE):
            with self.subTest(url=url):
                result = self._extract(self._page(self.clip), url, self._URL + '?sh=example')
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

    def test_no_formats_default_error(self):
        self.ydl.params['ignore_no_formats_error'] = False
        with self.assertRaisesRegex(ExtractorError, 'metadata only') as error:
            self._extract(self._page(self.clip))
        self.assertTrue(error.exception.expected)

    def test_unavailable_or_wrong_song(self):
        for page in ('<html></html>', self._page(dict(self.clip, is_public=False)),
                     self._page(dict(self.clip, is_public=1)), self._page(dict(self.clip, status='pending')),
                     self._page(dict(self.clip, id='different'))):
            with self.subTest(page=page), self.assertRaises(ExtractorError) as error:
                self._extract(page)
            self.assertTrue(error.exception.expected)
        with self.assertRaisesRegex(ExtractorError, 'Conflicting'):
            self._extract(self._page(self.clip, dict(self.clip, title='Conflicting title')))

    def test_invalid_redirect(self):
        for final_url in ('https://example.invalid/song/' + self._ID, self._SHARE,
                          'https://suno.com/song/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'):
            with self.subTest(final_url=final_url), self.assertRaises(ExtractorError):
                self._extract(self._page(self.clip), final_url=final_url)

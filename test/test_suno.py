import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from yt_dlp import YoutubeDL
from yt_dlp.extractor import SunoIE
from yt_dlp.extractor.suno import _FlightScripts
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

    def test_idless_resource_hints(self):
        hint = ':HL["/_next/static/css/app.css","style"]\n'
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        fake_hint = ':HL' + json.dumps(self._model(dict(self.clip, title='False song'))) + '\n'
        for row in (hint, fake_hint):
            with self.subTest(row=row):
                with self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
                    self._extract(self._flight_page(row))
                for flight in (row + genuine, genuine + row):
                    result = self._extract(self._flight_page(flight, split=2))
                    self.assertEqual(result['title'], self.clip['title'])

    def test_invalid_idless_rows(self):
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        for row in (':H\n', ':HL\n', ':HLnot-json\n', ':H1[]\n', ':D{}\n', ':{}\n'):
            with self.subTest(row=row), self.assertRaisesRegex(ExtractorError, 'Flight'):
                self._extract(self._flight_page(row + genuine))
        with self.assertRaisesRegex(ExtractorError, 'Truncated'):
            self._extract(self._flight_page(genuine + ':HL[]'))

    def test_malformed_flight_segments_fail_closed(self):
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        conflict = 'c:' + json.dumps(self._model(dict(self.clip, title='Conflicting title'))) + '\n'
        encoded_conflict = json.dumps([1, conflict])
        valid_page = self._flight_page(genuine)

        for bad_payload in (encoded_conflict[:-1] + ',]', encoded_conflict[:-1], 'not-json'):
            bad_segment = f'<script>self.__next_f.push({bad_payload})</script>'
            for page in (bad_segment, valid_page + bad_segment, bad_segment + valid_page):
                with self.subTest(payload=bad_payload, page=page), self.assertRaisesRegex(
                        ExtractorError, 'Invalid Suno Flight segment') as error:
                    self._extract(page)
                self.assertTrue(error.exception.expected)

        missing_close = f'<script>self.__next_f.push({encoded_conflict}</script>'
        with self.assertRaisesRegex(ExtractorError, 'Invalid Suno Flight segment'):
            self._extract(valid_page + missing_close)

        # Valid non-data segments remain ignorable, and valid multiline JSON is accepted.
        bootstrap = '<script>self.__next_f.push([0])</script>'
        model = f'<script>self.__next_f.push({json.dumps([1, genuine], indent=2)})</script>'
        result = self._extract(bootstrap + model)
        self.assertEqual(result['title'], self.clip['title'])

    def test_flight_segment_types(self):
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        valid_page = self._flight_page(genuine)
        for payload in ([1], [1, None], [1, 123], [1, {}], [1, []], [1, '', 'extra'],
                        [3, 'YQ=='], [3], [4, 'unknown'], [True, genuine], [1.0, genuine],
                        [], {}, [0, genuine], [2]):
            segment = f'<script>self.__next_f.push({json.dumps(payload)})</script>'
            for page in (segment, valid_page + segment, segment + valid_page):
                with self.subTest(payload=payload, page=page), self.assertRaisesRegex(
                        ExtractorError, 'Flight.*segment') as error:
                    self._extract(page)
                self.assertTrue(error.exception.expected)

        fake = json.dumps(self._model(dict(self.clip, title='False song')))
        for payload in ([0], [2, None], [2, {'state': fake}]):
            segment = f'<script>self.__next_f.push({json.dumps(payload)})</script>'
            for page in (segment + valid_page, valid_page + segment):
                with self.subTest(payload=payload):
                    self.assertEqual(self._extract(page)['title'], self.clip['title'])

    def test_duplicate_visibility_requires_boolean(self):
        for visibility in (1, 0, 1.0, 'true', None):
            malformed = dict(self.clip, is_public=visibility)
            for clips in ((malformed, self.clip), (self.clip, malformed)):
                with self.subTest(visibility=visibility, clips=clips), self.assertRaisesRegex(
                        ExtractorError, 'Invalid Suno visibility flag') as error:
                    self._extract(self._page(*clips))
                self.assertTrue(error.exception.expected)
        private = dict(self.clip, is_public=False)
        for clips in ((private, self.clip), (self.clip, private)):
            with self.subTest(clips=clips), self.assertRaisesRegex(ExtractorError, 'Conflicting'):
                self._extract(self._page(*clips))
        self.assertEqual(self._extract(self._page(self.clip, self.clip))['title'], self.clip['title'])

    def test_malformed_flight_rows_fail_closed(self):
        genuine = 'b:' + json.dumps(self._model(self.clip)) + '\n'
        for bad in ('a:Tx,hello', 'a:Tffff,hello', 'a:T1,é', 'garbage\n', 'a:{\n'):
            with self.subTest(bad=bad), self.assertRaisesRegex(ExtractorError, 'Flight'):
                self._extract(self._flight_page(bad + genuine))
        with self.assertRaisesRegex(ExtractorError, 'Truncated'):
            self._extract(self._flight_page(genuine.rstrip('\n')))

    def test_html_script_context(self):
        valid = self._flight_page('b:' + json.dumps(self._model(self.clip)) + '\n')
        conflict = 'c:' + json.dumps(self._model(dict(self.clip, is_public=False))) + '\n'
        encoded = json.dumps([1, conflict])
        for script in (f'<SCRIPT>\n self.__next_f.push ({encoded}); \n</SCRIPT>',
                       f'<script>self . __next_f . push({encoded})</script>'):
            with self.subTest(script=script), self.assertRaisesRegex(ExtractorError, 'Conflicting'):
                self._extract(valid + script)
        for script in (f'<script>/* prefix */self.__next_f.push({encoded})</script>',
                       f'<script>self.__next_f.push({encoded})'):
            with self.subTest(script=script), self.assertRaisesRegex(ExtractorError, 'Flight segment'):
                self._extract(valid + script)
        for inert in (f'<!--{valid}-->', f"<div data-text='{valid}'></div>",
                      f'<textarea>{valid}</textarea>', f'<template>{valid}</template>',
                      f'<title>{valid}</title>',
                      f'<script type="application/json">self.__next_f.push({encoded})</script>'):
            with self.subTest(inert=inert), self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
                self._extract(inert)
        self.assertEqual(self._extract(
            '<script>(self.__next_f=self.__next_f||[]).push([0]);</script>' + valid)['title'], self.clip['title'])

    def test_script_type_authority(self):
        public = self._flight_page('b:' + json.dumps(self._model(self.clip)) + '\n')
        private = self._flight_page('c:' + json.dumps(self._model(dict(self.clip, is_public=False))) + '\n')
        active = (
            'type="application/x-javascript"', 'type="text/jscript"', 'type="text/javascript1.5"',
            'type="text/javascript" type="application/json"', 'language="javascript"',
            'language="javascript" language="json"', 'type="" language="json"',
            'type="module"', 'type="module" nomodule', 'type="TEXT/JAVASCRIPT"',
            'type="text/javascript" language="json"',
            'type="MODULE"', 'type="Module"', 'type=" text/javascript "',
            'type=""', 'type language="json"', 'language="JavaScript"',
            'for="window" event="onload"', 'for=" WINDOW " event=" OnLoad() "',
        )
        for attrs in active:
            with self.subTest(attrs=attrs):
                self.assertEqual(self._extract(public.replace('<script>', f'<script {attrs}>'))['title'], self.clip['title'])
                conflict = private.replace('<script>', f'<script {attrs}>')
                for page in (public + conflict, conflict + public):
                    with self.assertRaisesRegex(ExtractorError, 'Conflicting'):
                        self._extract(page)

        inert = (
            'type="text/javascript; charset=utf-8"', 'type="module;invalid"',
            'type="application/json" type="text/javascript"', 'language="json"',
            'language="json" language="javascript"', 'nomodule', 'src=""',
            'type=" module"', 'type="module "', 'type="\tmodule\n"',
            'type=" "', 'type="\t"', 'language="javascript "',
            'language=" javascript"', 'language=" "', 'language="module"',
            'for="window" event="onclick"', 'for="example" event="onload"',
        )
        for attrs in inert:
            with self.subTest(attrs=attrs):
                with self.assertRaisesRegex(ExtractorError, 'metadata unavailable'):
                    self._extract(public.replace('<script>', f'<script {attrs}>'))
                conflict = private.replace('<script>', f'<script {attrs}>')
                for page in (public + conflict, conflict + public):
                    self.assertEqual(self._extract(page)['title'], self.clip['title'])

    def test_flight_discovery_limits(self):
        with patch.object(_FlightScripts, 'MAX_PAGE_BYTES', 100):
            with self.assertRaisesRegex(ExtractorError, 'webpage exceeds'):
                self._extract('é' * 60)
        with patch.object(_FlightScripts, 'MAX_SCRIPTS', 1):
            with self.assertRaisesRegex(ExtractorError, 'Too many'):
                self._extract(self._page(self.clip))
        with patch.object(_FlightScripts, 'MAX_FLIGHT_BYTES', 10):
            with self.assertRaisesRegex(ExtractorError, 'data exceeds'):
                self._extract(self._page(self.clip))
        with self.assertRaisesRegex(ExtractorError, 'unclosed script'):
            self._extract('<script>self.__next_f.push(' * 3200)

    def test_duplicate_json_members_and_nonfinite_numbers(self):
        model = json.dumps(self._model(self.clip))
        genuine = 'b:' + model + '\n'
        for value in (model.replace('"is_public": true', '"is_public": false, "is_public": true'),
                      model.replace('"duration": 231.6', '"duration": NaN'),
                      model.replace('"duration": 231.6', '"duration": 1e400')):
            with self.subTest(value=value), self.assertRaisesRegex(ExtractorError, 'Flight model'):
                self._extract(self._flight_page(genuine + 'c:' + value + '\n'))
        for payload in ('[2, {"x": 1, "x": 2}]', '[2, Infinity]'):
            with self.subTest(payload=payload), self.assertRaisesRegex(ExtractorError, 'Flight segment'):
                self._extract(self._flight_page(genuine) + f'<script>self.__next_f.push({payload})</script>')
        with self.assertRaisesRegex(ExtractorError, 'Flight hint'):
            self._extract(self._flight_page(genuine + ':HL{"x":1,"x":2}\n'))

    def test_query_identity(self):
        other = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        for key, candidate_id in ((['clips'], self._ID), (['clips', other], self._ID),
                                  (['clips', None], self._ID), (['clips', 7], self._ID),
                                  (['clips', self._ID, other], self._ID),
                                  (['clips', self._ID, other], other), (['clips', self._ID], other)):
            model = self._model(dict(self.clip, id=candidate_id))
            model[3]['prefetched']['dehydratedState']['queries'][0]['queryKey'] = key
            for models in ((model, self._model(self.clip)), (self._model(self.clip), model)):
                flight = ''.join(f'{i:x}:{json.dumps(value)}\n' for i, value in enumerate(models))
                with self.subTest(key=key, candidate_id=candidate_id), self.assertRaisesRegex(
                        ExtractorError, 'query identity'):
                    self._extract(self._flight_page(flight))
        self.assertEqual(self._extract(self._page(
            dict(self.clip, id=other), self.clip))['id'], self._ID)

    def test_malformed_outer_query_keys(self):
        for key in (None, 'clips', 1, {}, [], ['other', self._ID]):
            for missing in (False, True) if key is None else (False,):
                model = self._model(dict(self.clip, is_public=False))
                query = model[3]['prefetched']['dehydratedState']['queries'][0]
                if missing:
                    del query['queryKey']
                else:
                    query['queryKey'] = key
                for models in ((model, self._model(self.clip)), (self._model(self.clip), model)):
                    flight = ''.join(f'{i:x}:{json.dumps(value)}\n' for i, value in enumerate(models))
                    with self.subTest(key=key, missing=missing), self.assertRaisesRegex(
                            ExtractorError, 'query identity'):
                        self._extract(self._flight_page(flight))
        other = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        model = self._model(dict(self.clip, id=other))
        model[3]['prefetched']['dehydratedState']['queries'][0]['queryKey'] = ['other', self._ID]
        flight = 'a:' + json.dumps(model) + '\nb:' + json.dumps(self._model(self.clip)) + '\n'
        self.assertEqual(self._extract(self._flight_page(flight))['id'], self._ID)

    def test_duplicate_duration_types(self):
        for pair in ((True, 1), (False, 0), (1, 1.0)):
            for values in (pair, pair[::-1]):
                clips = [dict(self.clip, metadata={'duration': value}) for value in values]
                with self.subTest(values=values), self.assertRaisesRegex(ExtractorError, 'Conflicting'):
                    self._extract(self._page(*clips))

    def test_extreme_duration_is_omitted(self):
        for duration in (1e306, 10**300, 86401, -1, 0):
            with self.subTest(duration=duration):
                result = self._extract(self._page(dict(self.clip, metadata={'duration': duration})))
                self.assertNotIn('duration', result)
                self._process(result)

    def test_invalid_redirect(self):
        for final_url in ('https://example.invalid/song/' + self._ID, self._SHARE,
                          'http://suno.com/song/' + self._ID,
                          'https://suno.com/song/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'):
            with self.subTest(final_url=final_url), self.assertRaises(ExtractorError):
                self._extract(self._page(self.clip), final_url=final_url)

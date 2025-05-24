# tests/test_webpage_parser.py
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from src.parser.webpage_parser import WebpageParser
from src.parser.utils import is_audio_url # Important for one of the planned tests
from src import constants as K

# Minimal mock for aiohttp.ClientSession if real calls are not desired
class MockClientSession:
    async def __aenter__(self):
        # Mock the methods of session that WebpageParser uses, like get()
        # For this test, _get_content will be mocked, so session methods might not be directly called.
        return self
    async def __aexit__(self, exc_type, exc, tb):
        pass
    # Add other methods if WebpageParser directly uses them and _get_content isn't fully mocked

class TestWebpageParserAudioExtraction(unittest.TestCase):
    def setUp(self):
        self.base_url = "http://testhost.com"
        self.settings = { # Provide minimal required settings
            K.SETTING_USER_AGENT: "TestAgent/1.0",
            K.SETTING_PAGE_TIMEOUT: 10,
            K.SETTING_RETRY_COUNT: 1,
            K.SETTING_BYPASS_JS_REDIRECTS: False, # Keep it simple for these tests
            K.SETTING_MIN_IMG_WIDTH: 0, # Not relevant for audio
            K.SETTING_MIN_IMG_HEIGHT: 0, # Not relevant for audio
            # Add any other settings WebpageParser strictly needs
            K.SETTING_STAY_IN_DOMAIN: False, 
            K.SETTING_SEARCH_DEPTH: 1, 
            K.SETTING_STOP_WORDS: [], 
        }
        # Mock the external_session for WebpageParser
        self.mock_external_session = MockClientSession() # Use a more complete mock if needed

    def _run_async(self, coro):
        return asyncio.run(coro)

    @patch('src.parser.webpage_parser.WebpageParser._get_content')
    def test_extract_audio_from_audio_tags(self, mock_get_content):
        html_content = """
        <html><body>
            <audio src="audio1.mp3"></audio>
            <audio src="/path/to/audio2.wav?query=true"></audio>
            <audio><source src="audio3.ogg" type="audio/ogg"></audio>
            <audio><source src="http://otherdomain.com/audio4.aac"></audio>
            <audio src="nonaudio.txt"></audio> 
        </body></html>
        """
        mock_get_content.return_value = (html_content, None, "Success", 200) # Mock successful content fetch

        parser = WebpageParser(url=self.base_url, settings=self.settings, process_js=False, process_dynamic=False, external_session=self.mock_external_session)
        
        _, media_files, _, _, _ = self._run_async(parser.parse())

        audio_urls_found = {mf[1] for mf in media_files if mf[0] == 'audio'}
        
        self.assertIn(urljoin(self.base_url, "audio1.mp3"), audio_urls_found)
        self.assertIn(urljoin(self.base_url, "/path/to/audio2.wav?query=true"), audio_urls_found)
        self.assertIn(urljoin(self.base_url, "audio3.ogg"), audio_urls_found)
        self.assertIn("http://otherdomain.com/audio4.aac", audio_urls_found)
        self.assertNotIn(urljoin(self.base_url, "nonaudio.txt"), audio_urls_found)
        self.assertEqual(len(audio_urls_found), 4)

    @patch('src.parser.webpage_parser.WebpageParser._get_content')
    def test_extract_audio_from_a_tags(self, mock_get_content):
        html_content = """
        <html><body>
            <a href="track1.mp3">Track 1</a>
            <a href="/download/track2.wav">Track 2 WAV</a>
            <a href="http://example.com/music/track3.ogg">Track 3 OGG</a>
            <a href="notanaudio.html">Not Audio</a>
            <a href="anotheraudio.flac?download=true">Another Audio FLAC</a>
        </body></html>
        """
        mock_get_content.return_value = (html_content, None, "Success", 200)

        parser = WebpageParser(url=self.base_url, settings=self.settings, process_js=False, process_dynamic=False, external_session=self.mock_external_session)
        
        _, media_files, _, _, _ = self._run_async(parser.parse())

        audio_urls_found = {mf[1] for mf in media_files if mf[0] == 'audio'}
        
        self.assertIn(urljoin(self.base_url, "track1.mp3"), audio_urls_found)
        self.assertIn(urljoin(self.base_url, "/download/track2.wav"), audio_urls_found)
        self.assertIn("http://example.com/music/track3.ogg", audio_urls_found)
        self.assertIn(urljoin(self.base_url, "anotheraudio.flac?download=true"), audio_urls_found)
        self.assertNotIn(urljoin(self.base_url, "notanaudio.html"), audio_urls_found)
        self.assertEqual(len(audio_urls_found), 4)

    # Add a test for JS extraction if you want to be very thorough, though it's more complex to set up.
    # For now, focusing on HTML tags.

if __name__ == '__main__':
    unittest.main()

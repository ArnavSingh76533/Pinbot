import asyncio
import gzip
from io import BytesIO
import os
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from bot import Bot, Search, ServiceError, image_results, post_json, parse_pinterest_html, fetch_pinterest_html, load_config

CONFIG = {"BOT_TOKEN": "123:test_token"}

def html_images(count):
    return ''.join(f'<img src="https://i.pinimg.com/236x/aa/bb/photo{i}.jpg">' for i in range(count))

HTML = html_images(2)

class Photos(unittest.TestCase):
    def test_extract_originals_and_keep_thumbnails(self):
        photos = parse_pinterest_html(HTML)
        self.assertEqual(len(photos), 2)
        self.assertEqual(photos[0]['photo_url'], 'https://i.pinimg.com/originals/aa/bb/photo0.jpg')
        self.assertEqual(photos[0]['thumbnail_url'], 'https://i.pinimg.com/236x/aa/bb/photo0.jpg')
        self.assertEqual(photos[0]['type'], 'photo')
        self.assertNotIn('input_message_content', photos[0])

    def test_filter_deduplicate_and_handle_sizes(self):
        html = HTML + '''<img src="https://i.pinimg.com/474x/aa/bb/photo0.jpg">
        <img src="https://i.pinimg.com/75x75_RS/aa/bb/photo1.jpg">
        <img src="https://i.pinimg.com/originals/aa/bb/extra.jpeg">
        <img src="https://i.pinimg.com/236x/aa/bb/image.webp">
        <img src="https://i.pinimg.com.evil.test/236x/a.jpg">
        <img src="https://i.pinimg.com:invalid/236x/a.jpg">
        <img src="https://example.org/?i.pinimg.com/236x/a.jpg">
        <img><img src="file:///a.jpg">
        <script>var s = '<img src="https://i.pinimg.com/236x/script.jpg">';</script>'''
        photos = parse_pinterest_html(html)
        self.assertEqual(len(photos), 3)
        self.assertEqual(len({p['id'] for p in photos}), 3)

    def test_no_images_no_fabricated_results(self):
        self.assertEqual(parse_pinterest_html('<html>Please log in</html>'), [])

    def test_limit(self):
        self.assertEqual(len(parse_pinterest_html(html_images(450))), 400)

    def test_query_encoding_and_gzip(self):
        class Response(BytesIO):
            headers = {'Content-Encoding': 'gzip'}
        with patch('bot.urlopen', return_value=Response(gzip.compress(HTML.encode()))) as http:
            self.assertEqual(fetch_pinterest_html('Moon h2 & stars + रात'), HTML)
        request = http.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(request.full_url).query),
                         {'rs': ['typed'], 'q': ['Moon h2 & stars + रात']})
        self.assertEqual(urlsplit(request.full_url).hostname, 'in.pinterest.com')

    def test_pinterest_block_is_reported(self):
        with patch('bot.urlopen', side_effect=HTTPError('url', 403, 'blocked', {}, BytesIO())):
            with self.assertRaises(ServiceError) as caught:
                fetch_pinterest_html('cats')
        self.assertEqual(caught.exception.code, 403)

    def test_secrets_absent_from_errors(self):
        error = HTTPError('https://api.telegram.org/botSECRET/test', 429, 'bad', {},
                          BytesIO(b'{"parameters":{"retry_after":4}}'))
        with patch('bot.urlopen', side_effect=error):
            with self.assertRaises(ServiceError) as caught:
                post_json('https://api.telegram.org/botSECRET/test', {})
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 4)

    def test_only_bot_token_needed(self):
        with patch('bot.Path.exists', return_value=False), patch.dict(os.environ, CONFIG, clear=True):
            self.assertEqual(load_config(), CONFIG)


class Workflow(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_cache_reuse(self):
        search = Search()
        with patch('bot.fetch_pinterest_html', return_value=HTML) as fetch:
            a, b = await asyncio.gather(search.images('Moon h2', 1), search.images('Moon h2', 1))
        self.assertEqual(a, b)
        fetch.assert_called_once_with('Moon h2')

    async def test_real_pagination_slices_one_snapshot(self):
        search = Search()
        with patch('bot.fetch_pinterest_html', return_value=html_images(65)) as fetch:
            first, more1 = await search.images('Moon h2', 1)
            second, more2 = await search.images('Moon h2', 2)
            third, more3 = await search.images('Moon h2', 3)
        self.assertEqual((len(first), len(second), len(third)), (40, 25, 0))
        self.assertEqual((more1, more2, more3), (True, False, False))
        self.assertFalse({p['id'] for p in first} & {p['id'] for p in second})
        fetch.assert_called_once()

    async def test_inline_search_to_telegram(self):
        bot = Bot(CONFIG)
        bot.api = AsyncMock()
        with patch('bot.fetch_pinterest_html', return_value=HTML) as fetch:
            await bot.handle({'inline_query': {'id': 'q', 'from': {'id': 1}, 'query': 'Moon h2'}})
        fetch.assert_called_once_with('Moon h2')
        method, payload = bot.api.call_args.args
        self.assertEqual(method, 'answerInlineQuery')
        self.assertEqual(payload['results'][0]['type'], 'photo')
        self.assertEqual(payload['next_offset'], '')

    async def test_invalid_offsets_do_not_search(self):
        bot = Bot(CONFIG)
        bot.api = AsyncMock()
        bot.search.images = AsyncMock()
        for offset in ('bad', '-1', '0', '11', '999999', '²'):
            await bot.inline({'id': 'q', 'from': {'id': 1}, 'query': 'cats', 'offset': offset})
        bot.search.images.assert_not_called()

    async def test_search_failure_has_help(self):
        bot = Bot(CONFIG)
        bot.api = AsyncMock()
        bot.search.images = AsyncMock(side_effect=ServiceError('Pinterest', 403))
        await bot.inline({'id': 'q', 'from': {'id': 1}, 'query': 'cats'})
        payload = bot.api.call_args.args[1]
        self.assertEqual(payload['results'], [])
        self.assertEqual(payload['cache_time'], 0)
        self.assertIn('unavailable', payload['button']['text'])

    async def test_empty_page_is_not_cached(self):
        search = Search()
        with patch('bot.fetch_pinterest_html', side_effect=['<html></html>', HTML]) as fetch:
            self.assertEqual(await search.images('cats', 1), ([], False))
            self.assertEqual(len((await search.images('cats', 1))[0]), 2)
        self.assertEqual(fetch.call_count, 2)

    async def test_newest_typed_query_wins(self):
        bot = Bot(CONFIG)
        bot.api = AsyncMock()
        bot.search.images = AsyncMock(return_value=(parse_pinterest_html(HTML), False))
        first = asyncio.create_task(bot.inline({'id': 'old', 'from': {'id': 1}, 'query': 'dai'}))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(bot.inline({'id': 'new', 'from': {'id': 1}, 'query': 'daisy'}))
        await asyncio.gather(first, second)
        bot.search.images.assert_awaited_once_with('daisy', 1)

if __name__ == '__main__':
    unittest.main()

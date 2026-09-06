#!/usr/bin/env python3
"""Telegram inline photo gallery. Python 3.11+, no third-party dependencies."""
import argparse
import asyncio
from collections import OrderedDict
import getpass
import gzip
from html.parser import HTMLParser
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("gallery")
PAGE_SIZE = 40
MAX_PAGES = 10
MAX_IMAGES = PAGE_SIZE * MAX_PAGES


class ServiceError(Exception):
    def __init__(self, service, code=0, retry_after=0):
        # Never include request URLs, API keys, or provider response bodies.
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"{service} request failed (code {code})")


def post_json(url, payload, headers=None, timeout=10, service="API"):
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        retry = 0
        try:
            retry = int(json.load(error).get("parameters", {}).get("retry_after", 0))
        except (ValueError, TypeError, AttributeError):
            pass
        raise ServiceError(service, error.code, retry) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise ServiceError(service) from None


def load_config():
    path = ROOT / "config.json"
    config = json.loads(path.read_text()) if path.exists() else {}
    for key in ("BOT_TOKEN",):
        if key in os.environ:
            config[key] = os.environ[key]
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", config.get("BOT_TOKEN", "")):
        raise ValueError("Missing/invalid BOT_TOKEN. Run: python bot.py --setup")
    return {"BOT_TOKEN": config["BOT_TOKEN"]}


def setup():
    path = ROOT / "config.json"
    if path.exists() and input("Replace existing config.json? [y/N] ").lower() != "y":
        return
    config = {"BOT_TOKEN": getpass.getpass("BotFather token (hidden): ").strip()}
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", config["BOT_TOKEN"]):
        raise ValueError("A valid bot token is required; no file was written.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(config, file, indent=2)
    os.chmod(path, 0o600)
    print("Saved. Enable /setinline in BotFather, then run: python bot.py --check")


def web_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed ports before downstream URL parsing.
        return (parsed.scheme == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


def image_results(rows, limit=PAGE_SIZE):
    results, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("imageUrl")
        if not web_url(url):
            continue
        parsed = urlsplit(url)
        # InlineQueryResultPhoto requires JPEG. Do not send HTML pin pages.
        if not parsed.path.lower().endswith((".jpg", ".jpeg")):
            continue
        identity = parsed.netloc.lower() + parsed.path
        if identity in seen:
            continue
        seen.add(identity)
        thumb = row.get("thumbnailUrl")
        result = {"type": "photo", "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
                  "photo_url": url, "thumbnail_url": thumb if web_url(thumb) else url}
        # Supplying photo dimensions helps Telegram arrange the native gallery.
        for source, target in (("imageWidth", "photo_width"), ("imageHeight", "photo_height")):
            value = row.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 10000:
                result[target] = value
        results.append(result)
        if len(results) == limit:
            break
    return results


PINTEREST_HEADERS = {
    "sec-ch-ua": '\"Not=A?Brand\";v=\"99\", \"Android WebView\";v=\"151\", \"Chromium\";v=\"151\"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '\"Android\"',
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Linux; Android 16; RMX5116 Build/BP2A.250605.015; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/151.0.7922.199 Mobile Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "dnt": "1",
    "x-requested-with": "mark.via.gp",
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "sec-fetch-dest": "document",
    "accept-encoding": "gzip, deflate",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "priority": "u=0, i",
}


class PinterestImages(HTMLParser):
    """Same img/src extraction as the supplied snippet, using the built-in parser."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        src = dict(attrs).get("src")
        if not web_url(src):
            return
        parsed = urlsplit(src)
        if parsed.hostname != "i.pinimg.com" or parsed.port not in (None, 443):
            return
        # Preserve the fetched thumbnail; use the requested originals path on send.
        path = re.sub(r"^/\d+x(?:\d+)?(?:_RS)?/", "/originals/", parsed.path)
        if not path.startswith("/originals/"):
            return
        original = urlunsplit(("https", "i.pinimg.com", path, "", ""))
        self.rows.append({"imageUrl": original, "thumbnailUrl": src})


def parse_pinterest_html(html):
    parser = PinterestImages()
    parser.feed(html)
    return image_results(parser.rows, limit=MAX_IMAGES)


def fetch_pinterest_html(query):
    url = "https://in.pinterest.com/search/pins/?" + urlencode({"rs": "typed", "q": query})
    request = Request(url, headers=PINTEREST_HEADERS)
    try:
        with urlopen(request, timeout=7) as response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ServiceError("Pinterest response too large")
            encoding = response.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding == "deflate":
                try:
                    body = zlib.decompress(body)
                except zlib.error:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)
            elif encoding not in ("", "identity"):
                raise ServiceError("Pinterest unsupported response encoding")
            return body.decode("utf-8", errors="replace")
    except HTTPError as error:
        raise ServiceError("Pinterest", error.code) from None
    except (URLError, TimeoutError, OSError, EOFError, zlib.error):
        raise ServiceError("Pinterest") from None


class Search:
    def __init__(self):
        self.cache = OrderedDict()
        self.locks = [asyncio.Lock() for _ in range(32)]

    async def images(self, query, page):
        if not 1 <= page <= MAX_PAGES:
            return [], False
        # One HTML fetch per phrase. Pages slice that snapshot; no invented
        # Pinterest page parameter and no re-fetching page one on scroll.
        key = query.casefold()
        async with self.locks[hash(key) % len(self.locks)]:
            entry = self.cache.get(key)
            if entry and entry[0] > time.monotonic():
                self.cache.move_to_end(key)
                photos = entry[1]
            else:
                html = await asyncio.to_thread(fetch_pinterest_html, query)
                photos = parse_pinterest_html(html)
                if photos:
                    self.cache[key] = (time.monotonic() + 600, photos)
                    self.cache.move_to_end(key)
                    while len(self.cache) > 256:
                        self.cache.popitem(last=False)
            start = (page - 1) * PAGE_SIZE
            return photos[start:start + PAGE_SIZE], start + PAGE_SIZE < len(photos)


class Bot:
    def __init__(self, config):
        self.config = config
        self.search = Search()
        self.username = "YourBot"
        self.users = OrderedDict()

    async def api(self, method, payload=None, timeout=10):
        data = await asyncio.to_thread(post_json,
            f"https://api.telegram.org/bot{self.config['BOT_TOKEN']}/{method}",
            payload or {}, None, timeout, "Telegram")
        if not isinstance(data, dict) or not data.get("ok"):
            raise ServiceError("Telegram", data.get("error_code", 0) if isinstance(data, dict) else 0)
        return data["result"]

    async def answer(self, iq, results, next_offset="", message=None):
        body = {"inline_query_id": iq["id"], "results": results,
                "cache_time": 300 if results else 0, "is_personal": False,
                "next_offset": next_offset}
        if message:
            body["button"] = {"text": message, "start_parameter": "help"}
        await self.api("answerInlineQuery", body)

    async def inline(self, iq):
        query = " ".join(iq.get("query", "").split())[:200]
        if len(query) < 2:
            await self.answer(iq, [], message="Type what you want to find")
            return
        raw_offset = iq.get("offset", "")
        if raw_offset and (not raw_offset.isascii() or not raw_offset.isdigit() or len(raw_offset) > 2):
            await self.answer(iq, [])
            return
        page = int(raw_offset or "1")
        if not 1 <= page <= MAX_PAGES:
            await self.answer(iq, [])
            return
        uid = iq["from"]["id"]
        now = time.monotonic()
        self.users[uid] = (now, iq["id"])
        self.users.move_to_end(uid)
        while len(self.users) > 10000:
            self.users.popitem(last=False)
        # Wait briefly while the user types; always process the newest query.
        await asyncio.sleep(0.3)
        if self.users.get(uid) != (now, iq["id"]):
            await self.api("answerInlineQuery", {"inline_query_id": iq["id"],
                "results": [], "cache_time": 0, "is_personal": True})
            return
        try:
            async with asyncio.timeout(9):
                results, more = await self.search.images(query, page)
            await self.answer(iq, results, str(page + 1) if results and more else "",
                              None if results else "Pinterest returned no photos — try another phrase")
        except (ServiceError, TimeoutError) as error:
            LOG.warning("Inline search unavailable: %s", error)
            await self.answer(iq, [], message="Search unavailable — open help")

    async def handle(self, update):
        try:
            if "inline_query" in update:
                await self.inline(update["inline_query"])
            elif "message" in update and update["message"]["chat"]["type"] == "private":
                message = update["message"]
                text = (f"Photo Gallery 🔎\n\nIn any chat, type:\n@{self.username} daisy garden at night\n\n"
                        "Browse the photos, then tap one to send it.\n"
                        "Try: hot couple • cats • moon wallpaper\n\n"
                        "If search is unavailable, try another phrase or try again later. "
                        "If no gallery appears, enable /setinline in BotFather.")
                await self.api("sendMessage", {"chat_id": message["chat"]["id"], "text": text,
                    "reply_markup": {"inline_keyboard": [
                        [{"text": "🔎 Search photos", "switch_inline_query": ""}],
                        [{"text": "🌼 Daisy garden", "switch_inline_query_current_chat": "daisy garden at night"}],
                        [{"text": "💕 Couple photos", "switch_inline_query_current_chat": "hot couple"}]]}})
        except ServiceError as error:
            LOG.warning("Update failed: %s", error)
        except Exception as error:
            LOG.error("Update failed: %s", type(error).__name__)

    async def check(self):
        me = await self.api("getMe")
        self.username = me["username"]
        print(f"Telegram credentials OK: @{self.username}")
        inline_enabled = bool(me.get("supports_inline_queries"))
        print("Inline mode: " + ("enabled" if inline_enabled else "DISABLED — enable /setinline in BotFather"))
        results, _ = await self.search.images("daisy garden at night", 1)
        print(f"Pinterest HTML search: {len(results)} JPEG results on the first page (no search key).")
        webhook = await self.api("getWebhookInfo")
        if webhook.get("url"):
            print("A webhook is configured. Stop the existing deployment before using this polling bot.")
            return False
        return inline_enabled and bool(results)

    async def run(self):
        me = await self.api("getMe")
        self.username = me["username"]
        if not me.get("supports_inline_queries"):
            raise ValueError("Enable /setinline in BotFather first.")
        if (await self.api("getWebhookInfo")).get("url"):
            raise ValueError("Existing webhook found. Use a new bot or remove the old deployment first.")
        LOG.info("Running @%s. Press Ctrl+C to stop.", self.username)
        queue = asyncio.Queue(maxsize=32)

        async def worker():
            while True:
                update, received = await queue.get()
                try:
                    if time.monotonic() - received < 12:
                        await self.handle(update)
                finally:
                    queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(8)]
        offset, backoff = 0, 1
        try:
            while True:
                try:
                    updates = await self.api("getUpdates", {"offset": offset, "timeout": 25,
                        "limit": 32, "allowed_updates": ["inline_query", "message"]}, timeout=35)
                    backoff = 1
                    for update in updates:
                        await queue.put((update, time.monotonic()))
                        offset = update["update_id"] + 1
                except ServiceError as error:
                    if error.code in (401, 404, 409):
                        raise ValueError("Invalid bot token or another running bot instance (Telegram code "
                                         f"{error.code}).") from None
                    LOG.warning("Polling interrupted: %s; retrying.", error)
                    await asyncio.sleep(min(max(error.retry_after, backoff), 60))
                    backoff = min(backoff * 2, 30)
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--setup", action="store_true", help="Save credentials interactively")
    group.add_argument("--search-test", metavar="QUERY", help="Test Pinterest search without any token")
    group.add_argument("--check", action="store_true", help="Validate credentials and make one real search")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.setup:
            setup()
        elif args.search_test:
            results, more = asyncio.run(Search().images(args.search_test, 1))
            print(f"Pinterest: {len(results)} photos on page one; more cached results: {more}")
            for result in results:
                print(result["photo_url"])
            if not results:
                raise SystemExit(1)
        else:
            bot = Bot(load_config())
            if args.check:
                if not asyncio.run(bot.check()):
                    raise SystemExit(1)
            else:
                asyncio.run(bot.run())
    except (ValueError, ServiceError, OSError) as error:
        # Config parsing errors do not contain credential values.
        LOG.error("%s", error)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

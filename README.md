# Pin Gallery Bot — direct Pinterest search

Type `@YourBot Moon h2` in any Telegram chat, browse Pinterest photos, then tap one to send it. Each phrase is passed to Pinterest's search page dynamically.

No image-search API key or paid search service is used. Only your Telegram bot token is required. Python 3.11+ is enough; no third-party packages are needed.

## How the search works

This version implements the supplied Pinterest HTML method:

1. GET `https://in.pinterest.com/search/pins/` with `rs=typed` and `q=<your phrase>`.
2. Send the supplied mobile WebView headers; decode gzip/deflate responses.
3. Extract `<img src>` URLs hosted on `i.pinimg.com`.
4. Convert size folders such as `/236x/` and `/474x/` into `/originals/`.
5. Remove duplicates and return JPEG photo results in Telegram's native inline gallery. Preserve the fetched smaller image as the thumbnail.

Python's built-in `urllib` and `HTMLParser` perform the request and extraction equivalent to the supplied requests/BeautifulSoup snippet. No hard-coded Moon image list is used.

## Create and start the bot

1. Open https://t.me/BotFather, send `/newbot`, and choose an available bot username.
2. Copy the token privately.
3. Send `/setinline`, choose your bot, and enter `Search photos...` as the placeholder.
4. Extract the ZIP, open a terminal inside `pin-gallery-bot`, and run:

```bash
python3 bot.py --setup
python3 bot.py --check
python3 bot.py
```

Setup asks only for the bot token, without displaying it. The token is stored in `config.json`, with owner-only permissions where supported. Do not share that file. You can instead set the `BOT_TOKEN` environment variable.

If updating the earlier version, stop its process, replace the project files, and retain your existing `config.json`. Only `BOT_TOKEN` is read; other old fields are ignored. Run `--setup` to rewrite the configuration with only your bot token if desired.

`--check` validates the Telegram token, inline-mode setting and one Pinterest search. No messages are sent. A running webhook is detected without being removed.

## Test Pinterest without a bot token

```bash
python3 bot.py --search-test "Moon h2"
```

This fetches Pinterest's page and prints extracted original image URLs from the first result page. It exits unsuccessfully if the request fails or no usable photos are returned.

## Android / Termux

```bash
pkg update
pkg install python unzip
termux-setup-storage
cd ~/storage/downloads
unzip -o pin-gallery-bot.zip
cd pin-gallery-bot
python bot.py --setup
python bot.py --search-test "Moon h2"
python bot.py --check
termux-wake-lock
python bot.py
```

Allow storage access when Android asks. Keep Termux running and allow background activity; Android may still stop it. A server is more dependable for continuous operation. Stop with Ctrl+C, then use `termux-wake-unlock` if you enabled the wake lock.

## Use it

Open your bot and tap Start. In Saved Messages or another Telegram chat, type its actual username, a space, and your phrase:

```text
@YourActualBot Moon h2
@YourActualBot daisy garden at night
@YourActualBot hot couple
```

Wait for the gallery, scroll, then tap a photo. Telegram sends it with normal “via @YourActualBot” attribution. You can also use the search buttons in the bot's private chat.

## Results, scrolling and reliability

- Fetches only the image tags present in Pinterest's initial HTML response. It does not execute JavaScript or load Pinterest's later scroll batches. A page with 25 usable photos returns those 25.
- Up to 400 distinct JPEGs from one fetched page are retained. Telegram receives up to 40 per inline page; further pages slice the same cached snapshot without repeating the first page. No next page is advertised when the snapshot is exhausted.
- Search results cache for ten minutes in memory; Telegram can cache successful answers for five minutes. Failed or empty searches are not cached locally. Restarting clears the local cache.
- Includes a short typing debounce, bounded cache/queues, eight update workers and polling retry delays. Run one polling process per bot token.
- Pinterest can return a login page, challenge, rate limit, or HTML without image tags. The bot reports an unavailable/empty search; it does not bypass challenges or access controls. This HTML method depends on Pinterest continuing to expose images this way.
- Only JPEG photo links are included, as required by Telegram's inline photo result type. PNG/WebP/GIF/video results are skipped.
- Rewriting a URL to `/originals/` does not prove that it exists or fits Telegram's inline photo size limit. The original may be unavailable or exceed 5 MB. If a thumbnail appears but sending fails, select another photo. The bot does not download, convert or rehost images.
- Telegram controls the gallery's exact layout, theme and column count. The original bot's private backend and exact photo ranking remain unknown.
- Queries go to Telegram and Pinterest. User IDs and search cache are temporary in memory; no persistent query or photo database is created. Logs exclude query text and tokens. Image rights remain with their owners.

## Docker / server

Create `config.json` using setup, then run:

```bash
docker build -t pin-gallery-bot .
docker run -d --name pin-gallery-bot --restart unless-stopped \
  --mount type=bind,src="$(pwd)/config.json",dst=/app/config.json,readonly \
  pin-gallery-bot
```

The build excludes credentials. No inbound port is needed. Allow outbound HTTPS to Telegram and Pinterest; Telegram itself retrieves the `i.pinimg.com` photos. View logs with `docker logs pin-gallery-bot`. Stop with `docker stop pin-gallery-bot`.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| No inline panel | Enable `/setinline`, use the actual username, and keep the process running. |
| Empty Pinterest results | Run `--search-test "Moon h2"`; try another phrase or retry later. Pinterest may have returned no image tags or a login/challenge page. |
| Pinterest code 403/429 | The host refused or rate-limited the request; retry later. |
| Telegram code 401/404 | Check your bot token. |
| Telegram code 409 | Stop another instance or the old webhook deployment. |
| Thumbnail loads but sending fails | The original may be unavailable or too large; select another photo. |
| Gallery stops after about 25 photos | Those are the photos present in the fetched HTML; this method does not load Pinterest's JavaScript scrolling results. |

## Validation

```bash
python3 -m unittest discover -s tests -v
```

Fifteen automated tests cover extraction, original URL conversion, host filtering, duplicate removal, compressed responses, query encoding, bot-token-only configuration, caching, pagination, typing debounce and failures. Tests use simulated HTML/network responses. Live Telegram rendering and photo delivery still require your token and a manual test. See `VALIDATION.txt` for the live Pinterest request outcome from this build.

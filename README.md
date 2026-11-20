# Facebook Marketplace MCP Server

An MCP (Model Context Protocol) server that lets any AI tool — Claude, Cursor,
VS Code, Claude Code, etc. — search **Facebook Marketplace**.

It does **not** automate a browser. It replays Facebook's own internal GraphQL
protocol (the same `POST /api/graphql/` requests the website makes) using the
open-source [`facebook-gql-scraper`](https://github.com/XD-MHLOO/facebook-gql-scraper)
engine, and can route every request through **Geonode proxies** to avoid IP
bans and rate limits.

> Built on existing work rather than from scratch: the scraping engine is
> `facebook-gql-scraper` (MIT), which already handles doc-id auto-rediscovery,
> pagination, and full listing metadata. This project only wraps it in an MCP
> interface and adds Geonode proxy wiring.

## How it works

```
  AI tool (Claude / Cursor / ...)
          │  MCP (stdio JSON-RPC)
          ▼
   server.py ── search_marketplace / get_listing / status
          │
          ▼
   facebook-gql-scraper (GraphQL, no browser)
          │  requests via Geonode proxy (optional)
          ▼
   Facebook GraphQL API (your session cookies for auth)
```

## Prerequisites

- Python 3.10+ (tested on 3.12)
- A Facebook account (used to export session cookies — never stored in code)
- **Optional:** a [Geonode](https://geonode.com) Proxies plan (residential / ISP /
  rotating datacenter) for IP rotation

## Install

```bash
git clone https://github.com/Sniffr/facebook-mcp.git
cd facebook-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Verify the setup (no network calls to Facebook, just imports + config):

```bash
.venv/bin/python server.py --check          # config only
.venv/bin/python server.py --check --live   # also confirms your saved login works
```

Run the offline test suite (no Facebook traffic):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Get your Facebook cookies (automatic)

The server authenticates using your Facebook session cookies. The easiest way is
to let the helper open Chrome and grab them for you:

```bash
.venv/bin/python get_cookies.py
# or: .venv/bin/python server.py --login
```

1. A **Chrome window opens** (your installed Chrome, using a dedicated profile
   in `.chrome-profile/`).
2. **Log in to Facebook** in that window if prompted.
3. The script detects the login (`c_user` cookie) and **auto-saves** all
   facebook.com cookies to `facebook_cookies.json` — no copy-paste.

Run it again later and, because the profile is persistent, you'll usually still
be logged in, so it just re-exports instantly. The cookies file is written with
owner-only permissions (`600`).

### Reuse the login in your everyday Chrome

If you're already logged in to Facebook in your normal Chrome (144+), skip the
separate profile:

```bash
.venv/bin/python get_cookies.py --default-chrome
```

1. It opens `chrome://inspect/#remote-debugging` in your Chrome — switch
   remote debugging **on** there, and click **Allow** if Chrome asks.
2. It reads the facebook.com cookies (plus your Chrome's User-Agent) and saves
   them. If macOS blocks reading Chrome's profile folder, it falls back to
   port `9222` (override with `CHROME_DEBUG_PORT`).
3. **Switch remote debugging back off** afterwards — while it's on, any local
   app can control your browser.

Trade-off: the scraper then shares your everyday session, so if Facebook ever
objects to it, you'd be logged out of Chrome too. The dedicated profile keeps
those separate.

Both modes also write `facebook_cookies.browser.json` with the capturing
browser's User-Agent. The server replays requests with that identity (and
matching `sec-ch-ua` headers), because a session that suddenly appears from a
different OS/browser is a classic trigger for Facebook to log it out.

### Manual fallback

If you'd rather not use the browser helper, export cookies yourself:

1. **Cookie-Editor (Firefox/Chrome)** — visit facebook.com, open the extension,
   `Export` → `JSON`, save it as `facebook_cookies.json` in this folder.
2. **EditThisCookie (Chrome)** — same idea: export as JSON.

You need at least the `c_user` and `xs` cookies from a logged-in session.

## Configure Geonode proxies

Geonode proxies are optional but recommended (Facebook rate-limits and flags
aggressive scraping; rotating your exit IP helps a lot). In your Geonode
dashboard → **Proxy configuration**, copy your endpoint:

- `USERNAME` — dashboard username (add targeting flags to it, e.g. `-country-us`)
- `PASSWORD` — dashboard password
- `HOST` — generated endpoint host (IP or DNS)
- `PORT` — e.g. `9000` for rotating HTTP/HTTPS

Set **one** of these forms:

| Env var | Example |
|---|---|
| `GEONODE_PROXY_URL` | `http://user-country-us:pass@host:9000` |
| `GEONODE_PROXY_USERNAME` + `GEONODE_PROXY_PASSWORD` + `GEONODE_PROXY_HOST` + `GEONODE_PROXY_PORT` | `user-country-us` / `pass` / `host` / `9000` |

The credentials are URL-encoded for you when you use the split form. The full
proxy URL is passed straight through to the engine's `proxy=` parameter.

## Attach it to an AI tool

Point your MCP client at `server.py`. Example (Claude Code / Claude Desktop /
Cursor — see `mcp.example.json`):

```json
{
  "mcpServers": {
    "facebook-marketplace": {
      "command": "/path/to/facebook-mcp/.venv/bin/python",
      "args": ["/path/to/facebook-mcp/server.py"],
      "env": {
        "FACEBOOK_COOKIES_FILE": "/path/to/facebook-mcp/facebook_cookies.json",
        "GEONODE_PROXY_URL": "http://YOUR_USERNAME:YOUR_PASSWORD@YOUR_HOST:9000"
      }
    }
  }
}
```

Claude Code one-liner (`--scope user` makes it available in every project):

```bash
claude mcp add --scope user facebook-marketplace -- \
  /path/to/facebook-mcp/.venv/bin/python \
  /path/to/facebook-mcp/server.py
```

## Tools

### `search_marketplace`

Search by query, location, and filters.

| Param | Type | Default | Description |
|---|---|---|---|
| `query` | string | — | Search term (required) |
| `latitude` | number | `FB_DEFAULT_LATITUDE` (NYC) | Search centre latitude |
| `longitude` | number | `FB_DEFAULT_LONGITUDE` (NYC) | Search centre longitude |
| `radius_km` | int | 50 | Search radius (1–500) |
| `min_price` | number | — | Min price (listing currency) |
| `max_price` | number | — | Max price (listing currency) |
| `condition` | string | — | `new` / `used_like_new` / `used_good` / `used_fair` |
| `sort_by` | string | — | `CREATION_TIME_DESCEND` / `PRICE_DESCEND` / `PRICE_ASCEND` / `DISTANCE_ASCEND` |
| `days` | int | — | Only listings from the last N days |
| `limit` | int | 20 | Max results (1–100) |

Returns `query`, the `location` searched, `count`, and `listings`, each with: `id`, `title`, `price`, `currency`, `strikethrough_price`, `location`,
`url`, `is_live`/`is_sold`/`is_pending`, `category_id`, `delivery_types`,
`seller {id, name}`, and `photos`.

### `get_listing`

Full metadata for one listing: description, exact lat/long, attributes,
delivery options, seller info + ratings, photos, and videos.

| Param | Type | Description |
|---|---|---|
| `listing_id` | string | The numeric listing ID, or a `facebook.com/marketplace/item/<id>` URL |

### `authenticate`

Re-login when the session is missing or expired — ask your AI tool to "log in
to Facebook" (session errors from the other tools also point it here). It
first checks the saved session and returns `already_logged_in` if it still
works. Otherwise it opens Chrome, waits for you to log in, saves the new
cookies and browser identity, switches the running server to them, and
confirms with Facebook.

| Param | Type | Default | Description |
|---|---|---|---|
| `method` | string | `login_window` | `login_window`: a dedicated Chrome window (profile in `.chrome-profile/`), kept separate from your everyday browsing. `everyday_chrome`: reuse your normal Chrome's login (needs remote debugging on; see below) |
| `wait_seconds` | int | 180 | How long to wait for you to finish logging in |
| `force` | bool | false | Capture a new session even if the current one works |

Statuses: `logged_in`, `already_logged_in`, `waiting` (the login window/tab is
open — log in, then call it again), `needs_remote_debugging` (switch it on at
`chrome://inspect/#remote-debugging`, then call again), `rejected` (cookies
saved but Facebook didn't accept them), `error`.

### `status`

Reports config: engine version, cookie-file presence, whether a proxy is
configured (password masked), and the default location. No network calls
unless you pass `verify_session: true`, which loads facebook.com once and
reports whether the saved login is still valid.

All tools are marked read-only. Results come back as structured JSON; failures
come back as MCP errors (`isError: true`) with a message saying what to do,
e.g. re-run `get_cookies.py` when the session has expired.

## Configuration reference

Relative paths are resolved against the project folder, not the MCP client's
working directory, so the defaults work no matter where the client starts
the server.

| Env var | Default | Purpose |
|---|---|---|
| `FACEBOOK_COOKIES_FILE` | `facebook_cookies.json` | Cookies JSON path |
| `FACEBOOK_STATE_FILE` | `facebook_state.json` | Cache of discovered GraphQL doc-ids |
| `FB_USER_AGENT` | capturing browser's UA | Override the User-Agent (client hints follow it) |
| `FB_DEFAULT_LATITUDE` / `FB_DEFAULT_LONGITUDE` | `40.7128` / `-74.0060` | Search centre when the caller gives none |
| `FB_REQUEST_TIMEOUT` | `30` | Per-request HTTP timeout (seconds) |
| `FB_CURRENCY` | — | Preferred ISO code for ambiguous price symbols (`$`, `£`, `¥`) |
| `GEONODE_PROXY_URL` | — | Full Geonode proxy URL |
| `GEONODE_PROXY_{USERNAME,PASSWORD,HOST,PORT}` | — | Split proxy credentials |

## Troubleshooting

- **`Facebook cookies file not found`** — export cookies (see above) and point
  `FACEBOOK_COOKIES_FILE` at them.
- **`Session expired` / `logged-out`** — re-run `get_cookies.py`; Facebook
  invalidates sessions frequently. The running server notices the new cookies
  file automatically, so there's no need to restart your AI tool.
- **First search / first `get_listing` is slow (10–30 s)** — the engine
  rediscovers Facebook's rotating GraphQL query IDs by scanning the scripts on
  a Marketplace page (often 1,000+ bundles; the server fetches them 8 at a
  time). The IDs are cached in `facebook_state.json`, so later calls are fast.
- **Searches suddenly return nothing / "doc_id ... was not found"** — Facebook
  rotated its query IDs; the engine rediscovers them on the next call. Retry.
- **Prices in the wrong currency** — Facebook only sends formatted prices
  (`$25`). Ambiguous symbols are read as the most common currency (`$` → USD,
  `£` → GBP, `¥` → JPY). Set `FB_CURRENCY` (e.g. `CAD`) to prefer yours.
- **CAPTCHA / account flag** — slow down, and use a Geonode proxy (especially
  residential) so your real IP isn't burned.

## Important legal note

Automating Facebook Marketplace violates Facebook's Terms of Service. Use this
for personal, low-volume, read-only research on data you're allowed to access,
and at your own risk. The server is **read-only** — no messaging, listing
creation, or other writes.

## Credits

- [`facebook-gql-scraper`](https://github.com/XD-MHLOO/facebook-gql-scraper) (MIT) — GraphQL scraping engine
  (0.10.x; the server patches in a current `fb_dtsg` lookup, parallel bundle
  prefetching for doc-id discovery, and currency disambiguation)
- [`jdcodes1/facebook-marketplace-mcp`](https://github.com/jdcodes1/facebook-marketplace-mcp) — reference for MCP tool design (search/get-listing)
- [Geonode](https://geonode.com) — proxy provider

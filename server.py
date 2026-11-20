#!/usr/bin/env python3
"""Facebook Marketplace MCP server.

An MCP (Model Context Protocol) server that lets an AI tool search Facebook
Marketplace. It replays Facebook's internal GraphQL protocol directly (no
browser automation at runtime) using the open-source ``facebook-gql-scraper``
engine, and can route every request through Geonode proxies to avoid IP bans
and rate limits.

Auth is via your Facebook session cookies (see README for how to export them).

Configuration (environment variables, set them in your MCP client's ``env``).
Relative paths are resolved against this file's folder, not the client's
working directory.

  FACEBOOK_COOKIES_FILE    path to the cookies JSON (default: facebook_cookies.json)
  FACEBOOK_STATE_FILE      where discovered GraphQL doc-ids are cached
                           (default: facebook_state.json)
  FB_USER_AGENT            optional User-Agent override
  FB_DEFAULT_LATITUDE      default search latitude  (default: 40.7128, NYC)
  FB_DEFAULT_LONGITUDE     default search longitude (default: -74.0060, NYC)
  FB_REQUEST_TIMEOUT       per-request HTTP timeout in seconds (default: 30)

  Geonode proxy (any one of the two forms):

  GEONODE_PROXY_URL        full proxy URL, e.g. http://USER:PASS@host:9000
  -- or --
  GEONODE_PROXY_USERNAME   Geonode dashboard username (targeting flags included)
  GEONODE_PROXY_PASSWORD   Geonode dashboard password
  GEONODE_PROXY_HOST       Geonode endpoint host (IP or DNS)
  GEONODE_PROXY_PORT       port (default 9000 for rotating HTTP/HTTPS)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

# Keep all logging on stderr: stdout is reserved for the MCP JSON-RPC stream.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("fb-marketplace-mcp")

# The engine turns off TLS verification when a proxy is set (Geonode
# intercepts HTTPS), which makes urllib3 warn on every single request.
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip() != "":
            return v.strip()
    return default


def _resolve(path: str) -> str:
    """Resolve a relative path against the project folder.

    MCP clients launch servers from arbitrary working directories (Claude
    Desktop uses ``/``), so a bare ``facebook_cookies.json`` would otherwise
    never be found.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return str(p)


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Ignoring %s=%r (not a number); using %s", name, raw, default)
        return default


def build_proxy_url() -> Optional[str]:
    """Resolve a Geonode proxy URL from the environment, or None if unset."""
    url = _env("GEONODE_PROXY_URL", "PROXY_URL")
    if url:
        return url

    username = _env("GEONODE_PROXY_USERNAME", "PROXY_USERNAME")
    password = _env("GEONODE_PROXY_PASSWORD", "PROXY_PASSWORD")
    host = _env("GEONODE_PROXY_HOST", "PROXY_HOST")
    port = _env("GEONODE_PROXY_PORT", "PROXY_PORT", default="9000")

    if username and password and host:
        # URL-encode credentials so special characters don't break the URL.
        return f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
    return None


def redact_proxy(proxy: str) -> str:
    """Return the proxy URL with the password masked."""
    m = re.match(r"^([\w+.-]+)://([^@]+)@(.+)$", proxy)
    if not m:
        # No credentials in the URL (or unparseable); nothing secret to show.
        return proxy if "@" not in proxy else "***"
    scheme, auth, hostport = m.groups()
    user, _, _ = auth.partition(":")
    return f"{scheme}://{user}:***@{hostport}"


def cookies_file() -> str:
    return _resolve(_env("FACEBOOK_COOKIES_FILE", default="facebook_cookies.json"))


def state_file() -> str:
    return _resolve(_env("FACEBOOK_STATE_FILE", default="facebook_state.json"))


def browser_identity_file() -> str:
    """Sidecar written by get_cookies.py with the capturing browser's User-Agent."""
    root, _ = os.path.splitext(cookies_file())
    return root + ".browser.json"


def browser_identity() -> dict[str, Any]:
    try:
        with open(browser_identity_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def user_agent() -> Optional[str]:
    """FB_USER_AGENT, else the UA of the browser the cookies came from."""
    return _env("FB_USER_AGENT") or browser_identity().get("user_agent") or None


def client_hints(ua: str, product: Optional[str] = None) -> dict[str, str]:
    """``sec-ch-ua*`` headers consistent with a Chrome User-Agent.

    The engine hard-codes Windows / Chrome 152 client hints. Sending those
    alongside a Mac UA (or Mac-issued cookies) is exactly the kind of
    mismatch that makes Facebook log a session out.
    """
    m = re.search(r"Chrome/(\d+)", ua)
    if not m or "Edg/" in ua or "OPR/" in ua:
        return {}
    major = m.group(1)
    pm = re.search(r"Chrome/([\d.]+)", product or "")
    full = pm.group(1) if pm and pm.group(1).split(".")[0] == major else f"{major}.0.0.0"

    if "Android" in ua:
        platform = "Android"
    elif "CrOS" in ua:
        platform = "Chrome OS"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        platform = "macOS"
    elif "Windows" in ua:
        platform = "Windows"
    elif "Linux" in ua or "X11" in ua:
        platform = "Linux"
    else:
        platform = ""
    return {
        "sec-ch-ua": f'"Chromium";v="{major}", "Not?A_Brand";v="24", "Google Chrome";v="{major}"',
        "sec-ch-ua-full-version-list": (
            f'"Chromium";v="{full}", "Not?A_Brand";v="24.0.0.0", "Google Chrome";v="{full}"'
        ),
        "sec-ch-ua-platform": f'"{platform}"',
        "sec-ch-ua-mobile": "?1" if "Mobile" in ua else "?0",
    }


def default_location() -> tuple[float, float]:
    return (
        _env_float("FB_DEFAULT_LATITUDE", 40.7128),
        _env_float("FB_DEFAULT_LONGITUDE", -74.0060),
    )


def request_timeout() -> float:
    return _env_float("FB_REQUEST_TIMEOUT", 30.0)


# ---------------------------------------------------------------------------
# Engine (lazy singleton)
# ---------------------------------------------------------------------------

_ctx = None
_ctx_cookies_mtime: Optional[float] = None
_ctx_lock = threading.Lock()
_call_lock = threading.Lock()  # serializes GraphQL calls (state not thread-safe)


class ConfigError(RuntimeError):
    pass


def _apply_default_timeout(ctx, timeout: float) -> None:
    """Give every HTTP request a timeout.

    The engine calls ``session.get``/``session.post`` without one, so a dead
    proxy would hang a tool call forever.
    """
    original = ctx.session.request

    def request(method, url, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original(method, url, **kwargs)

    ctx.session.request = request


# Where Facebook has embedded the fb_dtsg CSRF token over time. The engine
# (0.8.4) only knows the first, which logged-in pages no longer contain.
_DTSG_PATTERNS = (
    re.compile(r'\{"name":"fb_dtsg","value":"([^"]+)"\}'),
    re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'"DTSGInitData",\[\],\{"token":"([^"]+)"'),
    re.compile(r'"dtsg":\{"token":"([^"]+)"'),
    re.compile(r'name="fb_dtsg" value="([^"]+)"'),
)


def extract_dtsg(html: str) -> Optional[str]:
    for pattern in _DTSG_PATTERNS:
        m = pattern.search(html)
        if m:
            return m.group(1)
    return None


def _apply_dtsg_fix(ctx) -> None:
    """Replace the engine's fb_dtsg lookup with one that knows current page formats."""
    from facebook_gql_scraper import DtsgTokenFetchError

    def _get_fb_dtsg() -> str:
        if ctx.fb_dtsg:
            return ctx.fb_dtsg
        html = ctx.get("https://www.facebook.com").text
        token = extract_dtsg(html)
        if not token:
            m = re.search(r'"USER_ID":"(\d+)"', html)
            if m and m.group(1) == "0":
                from facebook_gql_scraper import SessionExpiredError

                raise SessionExpiredError("Facebook served the logged-out homepage.")
            raise DtsgTokenFetchError("Could not find fb_dtsg in the Facebook homepage")
        ctx.fb_dtsg = token
        ctx._save_state()
        return token

    ctx._get_fb_dtsg = _get_fb_dtsg


# Facebook sends prices only as formatted strings ("$25"), and the engine maps
# the symbol to the alphabetically-first ISO code that uses it: "$" -> ARS,
# "£" -> FKP. Prefer the currencies those symbols almost always mean instead.
_CURRENCY_PREFERENCE = (
    "USD", "EUR", "GBP", "JPY", "CAD", "AUD", "NZD", "MXN", "INR", "SGD", "HKD", "ZAR",
)


def _apply_currency_fix() -> None:
    from types import SimpleNamespace

    import facebook_gql_scraper.unifiedFacebookParser as parser_module

    current = getattr(parser_module, "iso4217parse", None)
    if current is None or getattr(current, "_fb_mcp_shim", False):
        return
    real_parse = current.parse

    def parse(*args, **kwargs):
        result = real_parse(*args, **kwargs)
        if result and len(result) > 1:
            preferred = [c.upper() for c in [_env("FB_CURRENCY")] if c] + list(_CURRENCY_PREFERENCE)
            rank: dict[str, int] = {}
            for i, code in enumerate(preferred):
                rank.setdefault(code, i)
            # Stable sort: codes we have no opinion on keep the library's order.
            result = sorted(result, key=lambda c: rank.get(c.alpha3, len(rank)))
        return result

    parser_module.iso4217parse = SimpleNamespace(parse=parse, _fb_mcp_shim=True)


DISCOVERY_WORKERS = 8


def _apply_parallel_discovery(ctx, workers: int = DISCOVERY_WORKERS) -> None:
    """Prefetch script bundles concurrently during doc-id discovery.

    Engine 0.10 rediscovers each GraphQL doc-id by downloading every script
    bundle on a reference page *sequentially* (hundreds of files, minutes per
    operation). Browsers fetch these in parallel; we do the same, then let the
    engine evaluate them in its original order.
    """
    from concurrent.futures import ThreadPoolExecutor

    disc = getattr(ctx, "operation_discovery", None)
    if disc is None or not hasattr(disc, "_get_script_urls") or not hasattr(disc, "_fetch_bundle"):
        return  # engine internals changed; fall back to its own behaviour

    original_get_urls = disc._get_script_urls
    original_fetch = disc._fetch_bundle
    prefetched: dict[str, Any] = {}

    def _get(url):
        try:
            return ctx.get(url)
        except Exception as exc:  # noqa: BLE001 - re-raised in _fetch_bundle
            return exc

    def get_script_urls(text, page_url):
        urls = original_get_urls(text, page_url)
        todo = [u for u in urls if u not in prefetched]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for url, resp in zip(todo, pool.map(_get, todo)):
                prefetched[url] = resp
        log.info("Prefetched %d script bundles in %.1fs for doc-id discovery", len(todo), time.monotonic() - started)
        return urls

    def fetch_bundle(url, operation_name):
        resp = prefetched.pop(url, None)
        if resp is None:
            return original_fetch(url, operation_name)
        if isinstance(resp, Exception):
            raise resp
        resp.raise_for_status()
        source = resp.text
        if operation_name not in source and "relayprovider" not in source:
            return ""
        return disc._prepare_bundle(source)

    disc._get_script_urls = get_script_urls
    disc._fetch_bundle = fetch_bundle


def reset_context() -> None:
    """Drop the cached engine so the next call rebuilds it from scratch."""
    global _ctx, _ctx_cookies_mtime
    with _ctx_lock:
        _ctx = None
        _ctx_cookies_mtime = None


def get_context():
    """Build the facebook-gql-scraper context once, lazily.

    If the cookies file changes on disk (e.g. after re-running
    ``get_cookies.py``) the new cookies are picked up without a restart.
    """
    global _ctx, _ctx_cookies_mtime
    with _ctx_lock:
        cookies = cookies_file()
        if not os.path.exists(cookies):
            raise ConfigError(
                f"Facebook cookies file not found: {cookies}. "
                "Call the `authenticate` tool (or run `python get_cookies.py`) "
                "to open Chrome, log in, and save cookies automatically."
            )
        mtime = os.path.getmtime(cookies)

        if _ctx is not None:
            if mtime != _ctx_cookies_mtime:
                log.info("Cookies file changed on disk; reloading session cookies")
                _ctx.reload_cookies()
                _ctx.fb_dtsg = None  # the token belongs to the old session
                _ctx_cookies_mtime = mtime
            return _ctx

        try:
            from facebook_gql_scraper import FacebookContext
        except ImportError as exc:  # pragma: no cover - hard to hit
            raise ConfigError(
                "facebook-gql-scraper is not installed. Run: "
                "pip install -r requirements.txt"
            ) from exc

        proxy = build_proxy_url()
        log.info(
            "Creating Facebook context (cookies=%s, proxy=%s)",
            cookies,
            redact_proxy(proxy) if proxy else "unset (direct connection)",
        )
        kwargs: dict[str, Any] = dict(cookies_file=cookies, state_file=state_file(), quiet=True)
        if proxy:
            kwargs["proxy"] = proxy
        ua = user_agent()
        if ua:
            kwargs["user_agent"] = ua

        try:
            ctx = FacebookContext(**kwargs)
        except ValueError as exc:  # invalid JSON in the cookies file
            raise ConfigError(f"{exc}. Call the `authenticate` tool to log in again.") from exc

        if ua:
            hints = client_hints(ua, browser_identity().get("product"))
            if hints:
                ctx.session.headers.update(hints)
                # The engine's value is a Windows version number.
                ctx.session.headers.pop("sec-ch-ua-platform-version", None)
            log.info("Using browser identity: %s", ua)

        _apply_default_timeout(ctx, request_timeout())
        _apply_dtsg_fix(ctx)
        _apply_parallel_discovery(ctx)
        _apply_currency_fix()
        _ctx = ctx
        _ctx_cookies_mtime = mtime
        return _ctx


def _tool_error(exc: Exception, action: str) -> ToolError:
    """Translate engine / network failures into a message the model can act on."""
    import requests
    from facebook_gql_scraper import (
        DocIdStillInvalidError,
        DtsgTokenFetchError,
        DtsgTokenStillInvalidError,
        FacebookAccessException,
        FacebookNotFoundError,
        SchemaChangedError,
        SessionExpiredError,
    )

    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, ConfigError):
        return ToolError(str(exc))
    if isinstance(exc, SessionExpiredError):
        reset_context()
        return ToolError(
            "Facebook session is expired or logged out. Call the `authenticate` "
            "tool to log in again (it opens Chrome for the user), then retry. "
            f"({type(exc).__name__}: {exc})"
        )
    if isinstance(exc, (DtsgTokenFetchError, DtsgTokenStillInvalidError)):
        reset_context()
        return ToolError(
            "Could not get Facebook's request token (fb_dtsg). Retry once; if it "
            "keeps failing, call status with verify_session=true to check the "
            "login — Facebook may have changed its page format. "
            f"({type(exc).__name__}: {exc})"
        )
    if isinstance(exc, FacebookNotFoundError):
        return ToolError(f"Not found: {exc}")
    if isinstance(exc, FacebookAccessException):
        return ToolError(f"Facebook refused access: {exc}")
    if isinstance(exc, (DocIdStillInvalidError, SchemaChangedError)):
        return ToolError(
            "Facebook changed its internal API and the engine could not adapt "
            "automatically. Retry once; if it keeps failing, upgrade "
            f"facebook-gql-scraper. ({type(exc).__name__}: {exc})"
        )
    if isinstance(exc, requests.exceptions.ProxyError):
        return ToolError(
            f"Proxy error: check the Geonode proxy credentials and host. ({exc})"
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return ToolError(
            f"Request to Facebook timed out after {request_timeout():g}s. "
            "Retry, or raise FB_REQUEST_TIMEOUT."
        )
    if isinstance(exc, requests.exceptions.RequestException):
        return ToolError(f"Network error talking to Facebook: {exc}")

    log.exception("Unexpected failure while %s", action)
    return ToolError(f"Unexpected error while {action}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Serializers (read raw payloads directly; never trigger metadata fetches
# accidentally from search results)
# ---------------------------------------------------------------------------

LISTING_URL = "https://www.facebook.com/marketplace/item/{id}/"


def _location_text(d: dict) -> Optional[str]:
    """Engine 0.8 gives ``location_text``; 0.10 search results give ``location``."""
    if d.get("location_text"):
        return d["location_text"]
    loc = d.get("location") or {}
    parts = [loc.get("city"), loc.get("state")]
    text = ", ".join(p for p in parts if p)
    return text or ((loc.get("city_page") or {}).get("display_name"))


def _photo_urls(d: dict) -> list[str]:
    photos = list(d.get("photos") or [])
    if not photos and d.get("listing_photo"):
        photos = [d["listing_photo"]]  # 0.10 search results: one cover photo
    return [p["image_url"] for p in photos if (p or {}).get("image_url")]


def _serialize_search_listing(listing) -> dict:
    d = listing._data
    seller = d.get("seller") or {}
    seller_profile = seller.get("profile") or {}
    lid = d.get("id")
    return {
        "id": lid,
        "title": d.get("title"),
        "price": d.get("price"),
        "currency": d.get("currency"),
        "strikethrough_price": d.get("strikethrough_price"),
        "location": _location_text(d),
        "url": LISTING_URL.format(id=lid) if lid else None,
        "is_live": d.get("is_live"),
        "is_sold": d.get("is_sold"),
        "is_pending": d.get("is_pending"),
        "category_id": d.get("category_id"),
        "delivery_types": d.get("delivery_types"),
        "seller": {
            "id": seller_profile.get("id"),
            "name": seller_profile.get("name"),
        },
        "photos": _photo_urls(d),
    }


def _serialize_full_listing(listing) -> dict:
    d = listing._data
    seller = d.get("seller") or {}
    seller_profile = seller.get("profile") or {}
    seller_stats = seller.get("seller_stats") or {}
    combined = seller.get("seller_buyer_combined") or {}
    partner = d.get("partner") or {}
    partner_profile = partner.get("profile") or {}

    videos = [
        {
            "thumbnail": (v or {}).get("thumbnail_image_url"),
            "url": (v or {}).get("video_url"),
        }
        for v in (d.get("videos") or [])
    ]

    # The engine always emits a "partner" dict, filled with None for ordinary
    # (non-business) sellers. Only report it when there's a real partner.
    has_partner = bool(partner_profile.get("id") or partner.get("partner_brand_name"))

    lid = d.get("id")
    return {
        "id": lid,
        "title": d.get("title"),
        "description": d.get("description"),
        "price": d.get("price"),
        "currency": d.get("currency"),
        "strikethrough_price": d.get("strikethrough_price"),
        "discounts_text": d.get("discounts_text"),
        "location": _location_text(d),
        "latitude": d.get("latitude"),
        "longitude": d.get("longitude"),
        "url": d.get("url") or (LISTING_URL.format(id=lid) if lid else None),
        "post_id": d.get("post_id"),
        "creation_time": d.get("creation_time"),
        "category_id": d.get("category_id"),
        "attributes": d.get("attributes"),
        "delivery_types": d.get("delivery_types"),
        "is_shipping_offered": d.get("is_shipping_offered"),
        "is_live": d.get("is_live"),
        "is_sold": d.get("is_sold"),
        "is_pending": d.get("is_pending"),
        "is_hidden": d.get("is_hidden"),
        "inventory_count": d.get("inventory_count"),
        "product_details_type": d.get("product_details_type"),
        "shipping_cost_text": d.get("shipping_cost_text"),
        "delivery_window_text": d.get("delivery_window_text"),
        "product_details_page_uri": d.get("product_details_page_uri"),
        "photos": _photo_urls(d),
        "videos": videos,
        "seller": {
            "id": seller_profile.get("id"),
            "name": seller_profile.get("name"),
            "profile_picture_url": seller_profile.get("profile_picture_url"),
            # 0.10 moved joined_time from seller.profile to seller.
            "joined_time": seller.get("joined_time") or seller_profile.get("joined_time"),
            "is_verified": seller.get("verified_badge"),
            "ratings_average": seller_stats.get("five_star_ratings_average"),
            "ratings_count": seller_stats.get("five_star_total_rating_count_by_role"),
            "ratings_are_private": seller.get("seller_ratings_are_private"),
            "combined_ratings_average": combined.get("five_star_ratings_average"),
            "combined_ratings_count": combined.get(
                "five_star_total_rating_count_by_role"
            ),
        },
        "partner_seller": {
            "id": partner_profile.get("id"),
            "name": partner_profile.get("name"),
            "brand": partner.get("partner_brand_name"),
            "review_count": partner.get("seller_review_count"),
            "positive_ratings_pct": partner.get("seller_positive_ratings_pct"),
            "member_since": partner.get("seller_member_since"),
            "sold_listings": partner.get("number_of_sold_listings"),
        }
        if has_partner
        else None,
    }


def parse_listing_id(value: str) -> str:
    """Accept a bare numeric id or a full Marketplace item URL."""
    text = str(value).strip()
    m = re.search(r"/marketplace/item/(\d+)", text) or re.fullmatch(r"(\d+)", text)
    if not m:
        raise ToolError(
            f"Invalid listing id {value!r}: pass the numeric id or a "
            "facebook.com/marketplace/item/<id> URL."
        )
    return m.group(1)


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="facebook-marketplace",
    version="0.2.0",
    instructions=(
        "Search Facebook Marketplace via Facebook's internal GraphQL API, "
        "with optional Geonode proxy routing. Read-only. Use search_marketplace "
        "to find listings near a latitude/longitude, then get_listing for full "
        "details of one. If a call reports a missing or expired session, call "
        "authenticate: it opens Chrome so the user can log in, then saves the "
        "new session (tell the user to log in in the window that appears). "
        "Use status to diagnose setup problems."
    ),
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

Condition = Literal["new", "used_like_new", "used_good", "used_fair"]
SortBy = Literal["CREATION_TIME_DESCEND", "PRICE_DESCEND", "PRICE_ASCEND", "DISTANCE_ASCEND"]


@mcp.tool(
    title="Search Facebook Marketplace",
    description=(
        "Search Facebook Marketplace listings by query, location, and filters. "
        "Returns matching listings with id, title, price, currency, location, "
        "seller name, photos, and a link to each listing. Location defaults to "
        "the server's configured default (New York City unless overridden) — "
        "pass latitude/longitude to search elsewhere."
    ),
    annotations=_READ_ONLY,
)
def search_marketplace(
    query: Annotated[str, Field(min_length=1, description='Search term, e.g. "iphone 13".')],
    latitude: Annotated[
        Optional[float],
        Field(ge=-90, le=90, description="Latitude of the search centre. Omit for the default location."),
    ] = None,
    longitude: Annotated[
        Optional[float],
        Field(ge=-180, le=180, description="Longitude of the search centre. Omit for the default location."),
    ] = None,
    radius_km: Annotated[int, Field(ge=1, le=500, description="Search radius in kilometres.")] = 50,
    min_price: Annotated[
        Optional[float], Field(ge=0, description="Minimum price, in the listing's local currency.")
    ] = None,
    max_price: Annotated[
        Optional[float], Field(ge=0, description="Maximum price, in the listing's local currency.")
    ] = None,
    condition: Annotated[Optional[Condition], Field(description="Item condition filter.")] = None,
    sort_by: Annotated[Optional[SortBy], Field(description="Sort order. Omit for Facebook's relevance ranking.")] = None,
    days: Annotated[
        Optional[int], Field(ge=1, description="Only listings posted in the last N days.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum number of results to return.")] = 20,
) -> dict[str, Any]:
    """Search Facebook Marketplace."""
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ToolError("min_price must be less than or equal to max_price.")

    default_lat, default_lon = default_location()
    lat = default_lat if latitude is None else latitude
    lon = default_lon if longitude is None else longitude

    try:
        from facebook_gql_scraper import SearchEngine

        with _call_lock:
            ctx = get_context()
            it = SearchEngine(ctx).search_marketplace(
                query=query,
                filter_location_latitude=lat,
                filter_location_longitude=lon,
                filter_radius_km=radius_km,
                filter_price_lower_bound=int(min_price) if min_price is not None else 0,
                filter_price_upper_bound=int(max_price) if max_price is not None else 2147483647,
                item_condition=condition,
                last_n_days=days,
                sort_by=sort_by,
                is_stock_available=True,
            )

            results: list[dict] = []
            for listing in it:
                results.append(_serialize_search_listing(listing))
                if len(results) >= limit:
                    break
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the model
        raise _tool_error(exc, f"searching for {query!r}") from exc

    return {
        "query": query,
        "location": {"latitude": lat, "longitude": lon, "radius_km": radius_km},
        "count": len(results),
        "listings": results,
    }


@mcp.tool(
    title="Get Marketplace listing",
    description=(
        "Get full details for a single Facebook Marketplace listing by ID or URL: "
        "description, exact location, attributes, delivery options, seller "
        "info and ratings, photos, and videos."
    ),
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
)
def get_listing(
    listing_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Numeric Marketplace listing ID, or a facebook.com/marketplace/item/<id> URL.",
        ),
    ],
) -> dict[str, Any]:
    """Fetch complete metadata for one listing."""
    lid = parse_listing_id(listing_id)
    try:
        from facebook_gql_scraper import MarketplaceListing

        with _call_lock:
            ctx = get_context()
            listing = MarketplaceListing.from_listing_id(ctx, lid)
    except Exception as exc:  # noqa: BLE001
        raise _tool_error(exc, f"fetching listing {lid}") from exc
    return _serialize_full_listing(listing)


def _check_session() -> dict[str, Any]:
    """Load facebook.com with the saved cookies and report who is logged in."""
    try:
        with _call_lock:
            ctx = get_context()
            html = ctx.get("https://www.facebook.com/").text
    except Exception as exc:  # noqa: BLE001
        err = _tool_error(exc, "checking the Facebook session")
        return {"logged_in": False, "error": str(err)}

    m = re.search(r'"USER_ID":"(\d+)"', html)
    if m and m.group(1) != "0":
        return {"logged_in": True, "user_id": m.group(1)}
    if m:
        reset_context()
        return {
            "logged_in": False,
            "error": "Facebook treated the saved cookies as logged out. Call the `authenticate` tool to log in again.",
        }
    return {"logged_in": None, "error": "Could not tell from the page whether the session is valid."}


def _cookie_capture():
    """Import get_cookies.py (it lives next to this file, not on sys.path)."""
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    import get_cookies  # noqa: PLC0415

    return get_cookies


_auth_lock = threading.Lock()


@mcp.tool(
    title="Log in to Facebook",
    description=(
        "Re-authenticate when the Facebook session is missing or expired. First "
        "checks the saved session and does nothing if it still works (unless "
        "force=true). Otherwise opens Chrome for the user to log in and saves "
        "the new session; the server switches to it immediately. "
        "method=login_window (default) opens a dedicated Chrome window kept "
        "separate from the user's everyday browsing; method=everyday_chrome "
        "reuses the login in the user's normal Chrome (needs remote debugging "
        "switched on at chrome://inspect/#remote-debugging). Tell the user to "
        "log in in the window that appears. If the result status is 'waiting' "
        "or 'needs_remote_debugging', relay the message and call this again "
        "once the user says they're done."
    ),
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
)
def authenticate(
    method: Annotated[
        Literal["login_window", "everyday_chrome"],
        Field(description="login_window: dedicated Chrome window. everyday_chrome: reuse the user's normal Chrome login."),
    ] = "login_window",
    wait_seconds: Annotated[
        int, Field(ge=5, le=900, description="How long to wait for the user to finish logging in before returning.")
    ] = 180,
    force: Annotated[
        bool, Field(description="Capture a new session even if the current one still works.")
    ] = False,
) -> dict[str, Any]:
    """Check the session and, if needed, run a browser login to refresh it."""
    if not force and os.path.exists(cookies_file()):
        before = _check_session()
        if before.get("logged_in"):
            return {
                "status": "already_logged_in",
                "message": "The saved Facebook session still works; nothing to do.",
                "session": before,
            }

    if not _auth_lock.acquire(blocking=False):
        raise ToolError("A login is already in progress; finish it in Chrome, then call authenticate again.")
    try:
        capture = _cookie_capture()
        say = lambda msg: log.info("authenticate: %s", msg)  # noqa: E731 - never print: stdout is MCP
        if method == "everyday_chrome":
            result = capture.capture_from_everyday_chrome(wait_seconds, say=say)
        else:
            result = capture.capture_with_login_window(wait_seconds, say=say)
    except Exception as exc:  # noqa: BLE001
        log.exception("authenticate failed")
        raise ToolError(f"Login flow failed: {type(exc).__name__}: {exc}") from exc
    finally:
        _auth_lock.release()

    out: dict[str, Any] = {
        "status": result["status"],
        "method": method,
        "message": result["message"],
    }
    if not result.get("ok"):
        return out

    # New cookies (and maybe a new browser identity): rebuild the engine.
    reset_context()
    session = _check_session()
    out["session"] = session
    if session.get("logged_in"):
        out["status"] = "logged_in"
        out["message"] = "Logged in to Facebook; the new session is active. " + result["message"]
    else:
        out["status"] = "rejected"
        out["message"] = (
            "Saved the browser's cookies, but Facebook did not accept them as a "
            "logged-in session. Make sure the Facebook tab in that browser shows "
            "you logged in, then call authenticate with force=true."
        )
    return out


@mcp.tool(
    title="Server status",
    description=(
        "Report the server's configuration: whether the cookie file exists, "
        "whether a Geonode proxy is configured, and the default search location. "
        "Set verify_session=true to also make one request to facebook.com and "
        "confirm the saved login still works."
    ),
    annotations=_READ_ONLY,
)
def status(
    verify_session: Annotated[
        bool,
        Field(description="Also check with facebook.com that the saved login is still valid (one network request)."),
    ] = False,
) -> dict[str, Any]:
    """Return a configuration summary, optionally checking the live session."""
    import facebook_gql_scraper as fgs

    proxy = build_proxy_url()
    cookies = cookies_file()
    lat, lon = default_location()
    out: dict[str, Any] = {
        "engine": "facebook-gql-scraper",
        "engine_version": fgs.__version__,
        "cookies_file": cookies,
        "cookies_file_exists": os.path.exists(cookies),
        "state_file": state_file(),
        # Never echo the proxy password back to the client.
        "proxy_configured": bool(proxy),
        "proxy": redact_proxy(proxy) if proxy else None,
        "default_location": {"latitude": lat, "longitude": lon},
        "request_timeout_s": request_timeout(),
        "user_agent": user_agent() or "engine default (Windows Chrome)",
    }
    if verify_session:
        out["session"] = _check_session()
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _check() -> None:
    """Self-test mode: verify imports/config without talking to Facebook."""
    print("== facebook-marketplace-mcp self-test ==")
    try:
        import facebook_gql_scraper as fgs

        print(f"facebook-gql-scraper version: {fgs.__version__}")
        from facebook_gql_scraper import (  # noqa: F401
            FacebookContext,
            MarketplaceListing,
            SearchEngine,
        )

        print("engine imports: OK")
    except Exception as exc:  # noqa: BLE001
        print(f"engine imports: FAILED -> {exc}")
        sys.exit(1)

    cookies = cookies_file()
    print(f"cookies file: {cookies} (exists={os.path.exists(cookies)})")
    proxy = build_proxy_url()
    print(f"proxy: {redact_proxy(proxy) if proxy else 'unset (direct connection)'}")
    lat, lon = default_location()
    print(f"default location: {lat}, {lon}")

    if not os.path.exists(cookies):
        print("\nWARNING: cookies file missing. Searches will fail until you run")
        print("`python get_cookies.py` (opens Chrome, you log in, cookies auto-save).")
    elif "--live" in sys.argv:
        print("\nChecking the saved Facebook session (one request)...")
        print(f"session: {_check_session()}")
    print("\nSelf-test complete.")


def _login() -> None:
    """Open Chrome, let the user log in, and save Facebook session cookies."""
    sys.path.insert(0, str(BASE_DIR))
    import get_cookies  # noqa: PLC0415

    sys.exit(get_cookies.main())


def main() -> None:
    if "--login" in sys.argv:
        _login()
        return
    if "--check" in sys.argv:
        _check()
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Offline tests for the Facebook Marketplace MCP server.

None of these talk to Facebook. Run with:

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

PROXY_VARS = [
    "GEONODE_PROXY_URL", "PROXY_URL",
    "GEONODE_PROXY_USERNAME", "PROXY_USERNAME",
    "GEONODE_PROXY_PASSWORD", "PROXY_PASSWORD",
    "GEONODE_PROXY_HOST", "PROXY_HOST",
    "GEONODE_PROXY_PORT", "PROXY_PORT",
]


def clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


SEARCH_ITEM = {
    "id": "1234567890",
    "title": "Road bike",
    "category_id": "1",
    "currency": "USD",
    "price": 250,
    "strikethrough_price": None,
    "location_text": "Brooklyn, NY",
    "is_hidden": False,
    "is_live": True,
    "is_pending": False,
    "is_sold": False,
    "delivery_types": ["IN_PERSON"],
    "seller": {"profile": {"id": "42", "name": "Sam"}},
    "photos": [{"id": "p1", "image_url": "https://img/1.jpg"}],
    "videos": [],
}

FULL_ITEM = {
    **SEARCH_ITEM,
    "url": "https://www.facebook.com/marketplace/item/1234567890/",
    "description": "Barely used",
    "latitude": 40.6,
    "longitude": -73.9,
    "attributes": [{"name": "Condition", "value": "Used - good"}],
    "seller": {
        "profile": {"id": "42", "name": "Sam", "profile_picture_url": None, "joined_time": 1},
        "verified_badge": False,
        "seller_ratings_are_private": False,
        "seller_stats": {"five_star_ratings_average": 4.8, "five_star_total_rating_count_by_role": 12},
        "seller_buyer_combined": {"five_star_ratings_average": 4.9, "five_star_total_rating_count_by_role": 20},
    },
    # The engine always emits a partner dict, even for ordinary sellers.
    "partner": {
        "profile": {"id": None, "name": None, "profile_picture_url": None},
        "partner_brand_name": None,
        "seller_review_count": None,
        "seller_positive_ratings_pct": None,
    },
    "videos": [{"id": "v1", "thumbnail_image_url": "https://img/t.jpg", "video_url": "https://v/1.mp4"}],
}


class ConfigTests(unittest.TestCase):
    def test_proxy_unset(self):
        with clean_env():
            self.assertIsNone(server.build_proxy_url())

    def test_proxy_full_url_wins(self):
        with clean_env(GEONODE_PROXY_URL=" http://u:p@h:9000 ", GEONODE_PROXY_HOST="other"):
            self.assertEqual(server.build_proxy_url(), "http://u:p@h:9000")

    def test_proxy_split_form_is_url_encoded(self):
        with clean_env(
            GEONODE_PROXY_USERNAME="geonode_user-country-us",
            GEONODE_PROXY_PASSWORD="p@ss:w/rd",
            GEONODE_PROXY_HOST="proxy.example.com",
        ):
            self.assertEqual(
                server.build_proxy_url(),
                "http://geonode_user-country-us:p%40ss%3Aw%2Frd@proxy.example.com:9000",
            )

    def test_proxy_split_form_needs_all_parts(self):
        with clean_env(GEONODE_PROXY_USERNAME="u", GEONODE_PROXY_HOST="h"):
            self.assertIsNone(server.build_proxy_url())

    def test_redact_proxy_hides_password(self):
        self.assertEqual(server.redact_proxy("http://user:secret@h:9000"), "http://user:***@h:9000")
        self.assertNotIn("secret", server.redact_proxy("http://user:secret@h:9000"))
        self.assertEqual(server.redact_proxy("http://h:9000"), "http://h:9000")

    def test_relative_paths_resolve_against_project(self):
        with clean_env(FACEBOOK_COOKIES_FILE="facebook_cookies.json"):
            self.assertEqual(server.cookies_file(), str(ROOT / "facebook_cookies.json"))
        with clean_env(FACEBOOK_COOKIES_FILE="/tmp/x.json"):
            self.assertEqual(server.cookies_file(), "/tmp/x.json")

    def test_default_location_override(self):
        with clean_env(FB_DEFAULT_LATITUDE="-1.2921", FB_DEFAULT_LONGITUDE="36.8219"):
            self.assertEqual(server.default_location(), (-1.2921, 36.8219))
        with clean_env(FB_DEFAULT_LATITUDE="nope"):
            self.assertEqual(server.default_location()[0], 40.7128)


MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)


class BrowserIdentityTests(unittest.TestCase):
    def test_client_hints_match_mac_chrome(self):
        h = server.client_hints(MAC_UA, "Chrome/157.0.8103.51")
        self.assertEqual(h["sec-ch-ua-platform"], '"macOS"')
        self.assertIn('"Google Chrome";v="157"', h["sec-ch-ua"])
        self.assertIn("157.0.8103.51", h["sec-ch-ua-full-version-list"])
        self.assertEqual(h["sec-ch-ua-mobile"], "?0")

    def test_client_hints_skip_non_chrome(self):
        self.assertEqual(server.client_hints("Mozilla/5.0 (X11; Linux) Firefox/140.0"), {})

    def test_context_uses_saved_identity(self):
        with tempfile.TemporaryDirectory() as d:
            cookies = os.path.join(d, "c.json")
            with open(cookies, "w") as f:
                json.dump([{"name": "c_user", "value": "1"}, {"name": "xs", "value": "a"}], f)
            with open(os.path.join(d, "c.browser.json"), "w") as f:
                json.dump({"user_agent": MAC_UA, "product": "Chrome/157.0.8103.51"}, f)
            with clean_env(FACEBOOK_COOKIES_FILE=cookies, FACEBOOK_STATE_FILE=os.path.join(d, "s.json")):
                os.environ.pop("FB_USER_AGENT", None)
                server.reset_context()
                ctx = server.get_context()
                self.assertEqual(ctx.session.headers["user-agent"], MAC_UA)
                self.assertEqual(ctx.session.headers["sec-ch-ua-platform"], '"macOS"')
                self.assertNotIn("sec-ch-ua-platform-version", ctx.session.headers)
        server.reset_context()


class EngineFixTests(unittest.TestCase):
    def test_currency_symbols_resolve_to_likely_currency(self):
        import facebook_gql_scraper.unifiedFacebookParser as ufp

        server._apply_currency_fix()
        server._apply_currency_fix()  # idempotent
        with clean_env():
            self.assertEqual(ufp.iso4217parse.parse("$25")[0].alpha3, "USD")
            self.assertEqual(ufp.iso4217parse.parse("£10")[0].alpha3, "GBP")
            self.assertEqual(ufp.iso4217parse.parse("KSh 1,000")[0].alpha3, "KES")
        with clean_env(FB_CURRENCY="cad"):
            self.assertEqual(ufp.iso4217parse.parse("$25")[0].alpha3, "CAD")

    def test_extract_dtsg_current_and_legacy_formats(self):
        self.assertEqual(server.extract_dtsg('["DTSGInitialData",[],{"token":"NEW"},1]'), "NEW")
        self.assertEqual(server.extract_dtsg('{"name":"fb_dtsg","value":"OLD"}'), "OLD")
        self.assertIsNone(server.extract_dtsg("<html></html>"))

    def test_search_listing_from_engine_0_10_shape(self):
        d = {
            "id": "1", "title": "Bike", "price": 25.0, "currency": "USD",
            "location": {"city": "Brooklyn", "state": "NY"},
            "listing_photo": {"type": "photo", "id": "p", "image_url": "https://img/c.jpg"},
            "seller": {"profile": {"id": "42", "name": "Sam"}},
        }
        out = server._serialize_search_listing(SimpleNamespace(_data=d))
        self.assertEqual(out["location"], "Brooklyn, NY")
        self.assertEqual(out["photos"], ["https://img/c.jpg"])


class ParseListingIdTests(unittest.TestCase):
    def test_accepts_id_and_urls(self):
        self.assertEqual(server.parse_listing_id(" 1234567890 "), "1234567890")
        self.assertEqual(
            server.parse_listing_id("https://www.facebook.com/marketplace/item/1234567890/?ref=search"),
            "1234567890",
        )

    def test_rejects_garbage(self):
        with self.assertRaises(ToolError):
            server.parse_listing_id("not-an-id")


class SerializerTests(unittest.TestCase):
    def test_search_listing(self):
        out = server._serialize_search_listing(SimpleNamespace(_data=SEARCH_ITEM))
        self.assertEqual(out["id"], "1234567890")
        self.assertEqual(out["url"], "https://www.facebook.com/marketplace/item/1234567890/")
        self.assertEqual(out["seller"], {"id": "42", "name": "Sam"})
        self.assertEqual(out["photos"], ["https://img/1.jpg"])
        self.assertEqual(out["location"], "Brooklyn, NY")

    def test_full_listing_without_partner(self):
        out = server._serialize_full_listing(SimpleNamespace(_data=FULL_ITEM))
        self.assertEqual(out["description"], "Barely used")
        self.assertEqual(out["seller"]["ratings_average"], 4.8)
        self.assertIsNone(out["partner_seller"])
        self.assertEqual(out["videos"], [{"thumbnail": "https://img/t.jpg", "url": "https://v/1.mp4"}])

    def test_full_listing_with_partner(self):
        data = dict(FULL_ITEM, partner={"profile": {"id": "9", "name": "Shop"}, "partner_brand_name": "Shop Co"})
        out = server._serialize_full_listing(SimpleNamespace(_data=data))
        self.assertEqual(out["partner_seller"]["brand"], "Shop Co")


class ToolTests(unittest.TestCase):
    def setUp(self):
        server.reset_context()

    def test_search_maps_args_and_respects_limit(self):
        fake_engine = mock.MagicMock()
        fake_engine.return_value.search_marketplace.return_value = iter(
            [SimpleNamespace(_data=dict(SEARCH_ITEM, id=str(i))) for i in range(10)]
        )
        with clean_env(FB_DEFAULT_LATITUDE="-1.29", FB_DEFAULT_LONGITUDE="36.82"), \
                mock.patch.object(server, "get_context", return_value=object()), \
                mock.patch("facebook_gql_scraper.SearchEngine", fake_engine):
            out = server.search_marketplace(query="bike", max_price=300, condition="used_good", limit=3)

        self.assertEqual(out["count"], 3)
        self.assertEqual([l["id"] for l in out["listings"]], ["0", "1", "2"])
        kwargs = fake_engine.return_value.search_marketplace.call_args.kwargs
        self.assertEqual(kwargs["filter_location_latitude"], -1.29)
        self.assertEqual(kwargs["filter_location_longitude"], 36.82)
        self.assertEqual(kwargs["filter_price_lower_bound"], 0)
        self.assertEqual(kwargs["filter_price_upper_bound"], 300)
        self.assertEqual(kwargs["item_condition"], "used_good")

    def test_search_rejects_inverted_price_range(self):
        with self.assertRaises(ToolError):
            server.search_marketplace(query="bike", min_price=500, max_price=100)

    def test_session_expired_becomes_actionable_error(self):
        from facebook_gql_scraper import SessionExpiredError

        with mock.patch.object(server, "get_context", side_effect=SessionExpiredError("expired")):
            with self.assertRaises(ToolError) as cm:
                server.get_listing(listing_id="1234567890")
        self.assertIn("authenticate", str(cm.exception))

    def test_proxy_error_message(self):
        import requests

        err = server._tool_error(requests.exceptions.ProxyError("407"), "searching")
        self.assertIn("Geonode", str(err))

    def test_missing_cookies_file(self):
        with tempfile.TemporaryDirectory() as d, \
                clean_env(FACEBOOK_COOKIES_FILE=os.path.join(d, "missing.json")):
            with self.assertRaises(server.ConfigError):
                server.get_context()

    def test_context_gets_timeout_and_reloads_cookies(self):
        with tempfile.TemporaryDirectory() as d:
            cookies = os.path.join(d, "c.json")
            with open(cookies, "w") as f:
                json.dump([{"name": "c_user", "value": "1"}, {"name": "xs", "value": "a"}], f)
            with clean_env(
                FACEBOOK_COOKIES_FILE=cookies,
                FACEBOOK_STATE_FILE=os.path.join(d, "s.json"),
                FB_REQUEST_TIMEOUT="7",
            ):
                ctx = server.get_context()
                self.assertEqual(ctx.session.cookies.get("c_user"), "1")

                with mock.patch("requests.Session.send") as send:
                    ctx.session.get("https://example.invalid/")
                    self.assertEqual(send.call_args.kwargs["timeout"], 7.0)

                with open(cookies, "w") as f:
                    json.dump([{"name": "c_user", "value": "2"}, {"name": "xs", "value": "b"}], f)
                os.utime(cookies, (1, 1))  # force an mtime change
                ctx2 = server.get_context()
                self.assertIs(ctx, ctx2)
                self.assertEqual(ctx2.session.cookies.get("c_user"), "2")
        server.reset_context()


class AuthenticateToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cookies = os.path.join(self.tmp.name, "c.json")
        with open(self.cookies, "w") as f:
            json.dump([{"name": "c_user", "value": "1"}, {"name": "xs", "value": "a"}], f)
        self.env = clean_env(FACEBOOK_COOKIES_FILE=self.cookies)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        server.reset_context()

    def test_noop_when_session_still_works(self):
        capture = mock.MagicMock()
        with mock.patch.object(server, "_check_session", return_value={"logged_in": True, "user_id": "1"}), \
                mock.patch.object(server, "_cookie_capture", return_value=capture):
            out = server.authenticate()
        self.assertEqual(out["status"], "already_logged_in")
        capture.capture_with_login_window.assert_not_called()

    def test_relogin_via_login_window(self):
        capture = mock.MagicMock()
        capture.capture_with_login_window.return_value = {"ok": True, "status": "saved", "message": "Saved 9 cookies."}
        checks = iter([{"logged_in": False}, {"logged_in": True, "user_id": "1"}])
        with mock.patch.object(server, "_check_session", side_effect=lambda: next(checks)), \
                mock.patch.object(server, "_cookie_capture", return_value=capture), \
                mock.patch.object(server, "reset_context") as reset:
            out = server.authenticate(wait_seconds=30)
        self.assertEqual(out["status"], "logged_in")
        reset.assert_called()
        self.assertEqual(capture.capture_with_login_window.call_args.args[0], 30)

    def test_waiting_is_reported_without_switching_sessions(self):
        capture = mock.MagicMock()
        capture.capture_from_everyday_chrome.return_value = {
            "ok": False, "status": "needs_remote_debugging", "message": "Switch it on."}
        with mock.patch.object(server, "_check_session", return_value={"logged_in": False}), \
                mock.patch.object(server, "_cookie_capture", return_value=capture), \
                mock.patch.object(server, "reset_context") as reset:
            out = server.authenticate(method="everyday_chrome")
        self.assertEqual(out["status"], "needs_remote_debugging")
        reset.assert_not_called()

    def test_force_skips_the_check(self):
        capture = mock.MagicMock()
        capture.capture_with_login_window.return_value = {"ok": False, "status": "waiting", "message": "Log in."}
        with mock.patch.object(server, "_check_session") as check, \
                mock.patch.object(server, "_cookie_capture", return_value=capture):
            out = server.authenticate(force=True, wait_seconds=5)
        check.assert_not_called()
        self.assertEqual(out["status"], "waiting")


class CookieCaptureTests(unittest.TestCase):
    """get_cookies.py capture flows with Chrome mocked out."""

    LOGGED_IN = [{"name": "c_user", "value": "1", "domain": ".facebook.com"},
                 {"name": "xs", "value": "a", "domain": ".facebook.com"}]

    def setUp(self):
        import get_cookies

        self.gc = get_cookies
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "c.json")
        self.patches = [mock.patch.object(get_cookies, "COOKIES_OUT", self.out),
                        mock.patch.object(get_cookies, "POLL_SECONDS", 0)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_login_window_saves_cookies_and_identity(self):
        gc = self.gc
        with mock.patch.object(gc, "_read_port", return_value=1234), \
                mock.patch.object(gc, "_port_alive", return_value=True), \
                mock.patch.object(gc, "_launch_chrome") as launch, \
                mock.patch.object(gc, "_get_facebook_cookies", side_effect=[[], self.LOGGED_IN]), \
                mock.patch.object(gc, "_page_ws_url", return_value="ws://x"), \
                mock.patch.object(gc, "_cdp_call", return_value={"userAgent": "UA", "product": "Chrome/157"}):
            res = gc.capture_with_login_window(30)
        launch.assert_not_called()
        self.assertEqual(res["status"], "saved")
        self.assertEqual(oct(os.stat(self.out).st_mode & 0o777), "0o600")
        with open(os.path.join(self.tmp.name, "c.browser.json")) as f:
            self.assertEqual(json.load(f)["user_agent"], "UA")

    def test_login_window_times_out_as_waiting(self):
        gc = self.gc
        with mock.patch.object(gc, "_read_port", return_value=None), \
                mock.patch.object(gc, "_launch_chrome") as launch, \
                mock.patch.object(gc, "_wait_for_port", return_value=1234), \
                mock.patch.object(gc, "_get_facebook_cookies", return_value=[]):
            res = gc.capture_with_login_window(0)
        launch.assert_called_once()
        self.assertEqual(res["status"], "waiting")
        self.assertFalse(os.path.exists(self.out))

    def test_everyday_chrome_opens_login_tab_then_saves(self):
        gc = self.gc
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        calls = []

        def call(method, params=None):
            calls.append(method)
            if method == "Storage.getCookies":
                n = calls.count("Storage.getCookies")
                return {"cookies": [] if n == 1 else self.LOGGED_IN}
            if method == "Browser.getVersion":
                return {"userAgent": "UA", "product": "Chrome/157"}
            return {}

        conn.call.side_effect = call
        with mock.patch.object(gc, "_everyday_chrome_ws_url", return_value="ws://x"), \
                mock.patch.object(gc, "CDPConnection", return_value=conn):
            res = gc.capture_from_everyday_chrome(30)
        self.assertEqual(res["status"], "saved")
        self.assertIn("Target.createTarget", calls)

    def test_everyday_chrome_needs_remote_debugging(self):
        gc = self.gc
        with mock.patch.object(gc, "_everyday_chrome_ws_url", return_value=None), \
                mock.patch.object(gc, "_open_in_everyday_chrome") as opened:
            res = gc.capture_from_everyday_chrome(0)
        opened.assert_called_once_with(gc.INSPECT_URL)
        self.assertEqual(res["status"], "needs_remote_debugging")


class StdioProtocolTests(unittest.TestCase):
    """Launch server.py as an MCP client would (from /, not the project dir)."""

    def test_handshake_tools_and_errors(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run():
            with tempfile.TemporaryDirectory() as d:
                env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
                env["FACEBOOK_COOKIES_FILE"] = os.path.join(d, "missing.json")
                params = StdioServerParameters(
                    command=sys.executable, args=[str(ROOT / "server.py")], cwd="/", env=env
                )
                async with stdio_client(params) as (r, w):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        tools = {t.name: t for t in (await s.list_tools()).tools}
                        status = await s.call_tool("status", {})
                        search = await s.call_tool("search_marketplace", {"query": "bike"})
                        bad = await s.call_tool("search_marketplace", {"query": "bike", "condition": "broken"})
                        return tools, status, search, bad

        tools, status, search, bad = asyncio.run(run())

        self.assertEqual(set(tools), {"search_marketplace", "get_listing", "status", "authenticate"})
        self.assertFalse(tools["authenticate"].annotations.read_only_hint)
        schema = tools["search_marketplace"].input_schema
        self.assertEqual(schema["required"], ["query"])
        self.assertIn("description", schema["properties"]["radius_km"])
        self.assertIn("used_good", json.dumps(schema["properties"]["condition"]))
        self.assertTrue(tools["get_listing"].annotations.read_only_hint)

        self.assertFalse(status.is_error)
        self.assertFalse(status.structured_content["cookies_file_exists"])

        self.assertTrue(search.is_error)
        self.assertIn("authenticate", search.content[0].text)

        self.assertTrue(bad.is_error)


if __name__ == "__main__":
    unittest.main()

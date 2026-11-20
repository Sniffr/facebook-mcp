#!/usr/bin/env python3
"""Capture Facebook session cookies automatically via Chrome DevTools Protocol.

Two ways to get a logged-in session, both reading cookies **in plaintext**
straight out of Chrome's DevTools Protocol — no Keychain access, no Full Disk
Access, no decryption, no browser-automation SDK:

* **Login window** (default): opens Chrome with a dedicated local profile in
  ``.chrome-profile/``, waits for you to log in to Facebook there, then saves
  the cookies. The profile is persistent, so re-running usually finds you still
  logged in and just re-exports instantly. The scraper's session stays
  separate from your everyday browsing.
* **Everyday Chrome** (``--default-chrome``): reuses the Facebook login in the
  Chrome you already use. You switch on remote debugging at
  chrome://inspect/#remote-debugging (Chrome 144+) and click Allow; if that
  Chrome isn't logged in to Facebook, a login tab opens there. Switch remote
  debugging back off afterwards.

Both also save the browser's User-Agent next to the cookies
(``facebook_cookies.browser.json``) so the server can present the same
browser identity to Facebook.

The MCP server's ``authenticate`` tool calls ``capture_with_login_window`` /
``capture_from_everyday_chrome`` directly; this file is also a CLI:

    python get_cookies.py                  # login window: capture / re-export
    python get_cookies.py --open           # force-open the login window
    python get_cookies.py --default-chrome # reuse your everyday Chrome's login

Environment (relative paths resolve against this file's folder):
    FACEBOOK_COOKIES_FILE   output path (default: facebook_cookies.json)
    CHROME_PROFILE_DIR      local profile dir (default: .chrome-profile)
    CHROME_BINARY           Chrome executable (default: macOS Google Chrome)
    CHROME_DEBUG_PORT       everyday Chrome's debugging port (default 9222)
    LOGIN_TIMEOUT           seconds to wait for login on the CLI (default 600)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve(path: str) -> str:
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


COOKIES_OUT = _resolve(os.environ.get("FACEBOOK_COOKIES_FILE") or "facebook_cookies.json")
PROFILE_DIR = _resolve(os.environ.get("CHROME_PROFILE_DIR") or ".chrome-profile")
LOGIN_TIMEOUT = int(os.environ.get("LOGIN_TIMEOUT") or "600")

CHROME_BINARY = (
    os.environ.get("CHROME_BINARY")
    or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)

DEFAULT_CHROME_DIR = os.path.expanduser(
    os.environ.get("DEFAULT_CHROME_DIR") or "~/Library/Application Support/Google/Chrome"
)
INSPECT_URL = "chrome://inspect/#remote-debugging"
LOGIN_URL = "https://www.facebook.com/login/"
DEBUG_PORT = int(os.environ.get("CHROME_DEBUG_PORT") or "9222")
POLL_SECONDS = 3

Say = Callable[[str], None]


def _quiet(_msg: str) -> None:
    pass


def _chrome_binary() -> str:
    if not os.path.exists(CHROME_BINARY):
        raise FileNotFoundError(
            "Google Chrome not found at " + CHROME_BINARY + " (set CHROME_BINARY)"
        )
    return CHROME_BINARY


# ---------------------------------------------------------------------------
# CDP client (uses the `websockets` library for reliability).
# ---------------------------------------------------------------------------


class CDPConnection:
    """A single DevTools websocket, reused for several calls.

    Reusing one connection matters for everyday Chrome: each new connection
    can make Chrome ask the user to Allow remote debugging again.
    """

    def __init__(self, ws_url: str, *, origin: str | None = "http://localhost", timeout: float = 10):
        import websockets.sync.client as wsc

        self.timeout = timeout
        self._next_id = 0
        self._ws = wsc.connect(
            ws_url,
            origin=origin,
            compression=None,
            open_timeout=timeout,
            # A whole browser's cookie jar easily exceeds the 1 MiB default
            # frame limit; we filter down to facebook.com after receiving it.
            max_size=64 * 1024 * 1024,
        )

    def call(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._ws.recv(timeout=self.timeout))
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} failed: {msg['error']}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _cdp_call(ws_url: str, method: str, params: dict | None = None, **kwargs) -> dict:
    with CDPConnection(ws_url, **kwargs) as conn:
        return conn.call(method, params)


# ---------------------------------------------------------------------------
# Cookie handling
# ---------------------------------------------------------------------------


def _facebook_only(raw: list[dict]) -> list[dict]:
    cookies = []
    for c in raw:
        domain = (c.get("domain") or "").lower()
        if "facebook.com" in domain:
            cookies.append(
                {
                    "name": c.get("name"),
                    "value": c.get("value"),
                    "domain": c.get("domain"),
                    "host": c.get("domain"),
                    "path": c.get("path"),
                    "expires": c.get("expires"),
                    "secure": bool(c.get("secure")),
                    "httpOnly": bool(c.get("httpOnly")),
                }
            )
    return cookies


def _cookie(cookies: list[dict], name: str) -> str | None:
    for c in cookies:
        if c.get("name") == name and c.get("value"):
            return c["value"]
    return None


def _logged_in(cookies: list[dict]) -> bool:
    return bool(_cookie(cookies, "c_user")) and bool(_cookie(cookies, "xs"))


def _browser_file() -> str:
    root, _ = os.path.splitext(COOKIES_OUT)
    return root + ".browser.json"


def _save(cookies: list[dict]) -> None:
    # Session cookies are as good as a password: keep the file private.
    fd = os.open(COOKIES_OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    os.chmod(COOKIES_OUT, 0o600)


def _save_browser_identity(version: dict, say: Say) -> None:
    """Record the browser's real User-Agent next to the cookies.

    Facebook can kill a session that suddenly shows up from a different
    browser/OS than the one that logged in, so the server replays requests
    with the same identity.
    """
    info = {"user_agent": version.get("userAgent"), "product": version.get("product")}
    with open(_browser_file(), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    say(f"Saved browser identity ({info['product']}) to {_browser_file()}")


def _result(status: str, message: str, **extra: Any) -> dict:
    return {"ok": status == "saved", "status": status, "message": message, **extra}


def _saved(cookies: list[dict], version: dict | None, say: Say, extra_note: str = "") -> dict:
    _save(cookies)
    if version:
        try:
            _save_browser_identity(version, say)
        except OSError as exc:
            say(f"(Could not save the browser identity: {exc})")
    msg = f"Saved {len(cookies)} facebook.com cookies to {COOKIES_OUT}."
    if extra_note:
        msg += " " + extra_note
    return _result("saved", msg, cookies_file=COOKIES_OUT, cookie_count=len(cookies))


# ---------------------------------------------------------------------------
# Mode 1: dedicated login window (.chrome-profile)
# ---------------------------------------------------------------------------


def _devtools_port_file() -> str:
    return os.path.join(PROFILE_DIR, "DevToolsActivePort")


def _read_port() -> int | None:
    try:
        with open(_devtools_port_file(), "r", encoding="utf-8") as f:
            first = f.readline().strip()
            return int(first) if first else None
    except (FileNotFoundError, ValueError):
        return None


def _port_alive(port: int) -> bool:
    """True if a Chrome DevTools endpoint is actually answering on ``port``.

    ``DevToolsActivePort`` is left behind when Chrome quits, so its mere
    presence doesn't mean Chrome is still running.
    """
    import requests

    try:
        requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).raise_for_status()
        return True
    except requests.RequestException:
        return False


def _launch_chrome() -> None:
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # Remove a stale port file so _wait_for_port() waits for the new one.
    try:
        os.remove(_devtools_port_file())
    except FileNotFoundError:
        pass
    subprocess.Popen(
        [
            _chrome_binary(),
            f"--user-data-dir={PROFILE_DIR}",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "https://www.facebook.com/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # keep the window open if our caller exits
    )


def _wait_for_port(timeout: float = 30.0) -> int:
    start = time.time()
    while time.time() - start < timeout:
        port = _read_port()
        if port:
            return port
        time.sleep(0.3)
    raise TimeoutError("Chrome did not start a DevTools port in time.")


def _page_ws_url(port: int) -> str:
    """Return a page target's websocket URL via the HTTP /json endpoint."""
    import requests

    targets = requests.get(f"http://127.0.0.1:{port}/json", timeout=5).json()
    for t in targets:
        if t.get("type") == "page":
            return t["webSocketDebuggerUrl"]
    ver = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()
    return ver["webSocketDebuggerUrl"]


def _get_facebook_cookies(port: int) -> list[dict]:
    result = _cdp_call(_page_ws_url(port), "Network.getAllCookies")
    return _facebook_only(result.get("cookies", []))


def capture_with_login_window(
    timeout: float = LOGIN_TIMEOUT, *, force_open: bool = False, say: Say = _quiet
) -> dict:
    """Open (or reuse) the dedicated Chrome login window and save its cookies.

    Returns ``{"ok", "status", "message", ...}`` where status is ``saved``,
    ``waiting`` (window open, not logged in yet — call again) or ``error``.
    """
    try:
        port = _read_port()
        opened = False
        if port is None or force_open or not _port_alive(port):
            _launch_chrome()
            opened = True
            say("Opened a Chrome login window (dedicated profile).")
            port = _wait_for_port()
    except (OSError, TimeoutError) as exc:
        return _result("error", f"Could not start Chrome: {exc}")

    say(f"Waiting up to {timeout:.0f}s for a Facebook login in that window...")
    deadline = time.time() + timeout
    while True:
        try:
            cookies = _get_facebook_cookies(port)
        except Exception as exc:  # noqa: BLE001 - Chrome may restart the target
            cookies = []
            say(f"(waiting for DevTools... {exc})")
        if _logged_in(cookies):
            version = None
            try:
                version = _cdp_call(_page_ws_url(port), "Browser.getVersion")
            except Exception as exc:  # noqa: BLE001
                say(f"(Could not read the browser's User-Agent: {exc})")
            return _saved(cookies, version, say, "You can close the Chrome login window.")
        if time.time() >= deadline:
            return _result(
                "waiting",
                ("A Chrome login window is open" if opened else "The Chrome login window is still open")
                + " — log in to Facebook there, then try again.",
            )
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Mode 2: everyday Chrome (chrome://inspect remote debugging, Chrome 144+)
# ---------------------------------------------------------------------------


def _tcp_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _everyday_chrome_ws_url(say: Say = _quiet) -> str | None:
    """Browser websocket URL of your everyday Chrome, if remote debugging is on.

    Chrome writes ``DevToolsActivePort`` (port, then browser ws path) into its
    user data dir when remote debugging is switched on. macOS privacy
    protection (or a sandbox) can block reading that folder; Chrome accepts
    the browser socket without the id, so fall back to the known port.
    """
    try:
        with open(os.path.join(DEFAULT_CHROME_DIR, "DevToolsActivePort"), encoding="utf-8") as f:
            parts = f.read().split()
        if len(parts) >= 2 and parts[0].isdigit() and _tcp_open(int(parts[0])):
            return f"ws://127.0.0.1:{parts[0]}{parts[1]}"
        return None
    except FileNotFoundError:
        return None
    except PermissionError:
        pass
    if _tcp_open(DEBUG_PORT):
        say(f"(Can't read Chrome's port file; using port {DEBUG_PORT} — set CHROME_DEBUG_PORT if different.)")
        return f"ws://127.0.0.1:{DEBUG_PORT}/devtools/browser"
    return None


def _open_in_everyday_chrome(url: str) -> None:
    # Handing a URL to the Chrome binary while Chrome is running opens it as a
    # new tab in the existing window (same profile, nothing restarted).
    subprocess.Popen(
        [_chrome_binary(), url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def capture_from_everyday_chrome(timeout: float = LOGIN_TIMEOUT, *, say: Say = _quiet) -> dict:
    """Save the Facebook cookies from the Chrome you already use.

    Status is ``saved``, ``needs_remote_debugging`` (the inspect page was
    opened; switch it on, then call again), ``waiting`` (a Facebook login tab
    is open in Chrome; log in, then call again) or ``error``.
    """
    deadline = time.time() + timeout

    ws_url = _everyday_chrome_ws_url(say)
    if ws_url is None:
        try:
            _open_in_everyday_chrome(INSPECT_URL)
        except OSError as exc:
            return _result("error", f"Could not open Chrome: {exc}")
        say(f"Opened {INSPECT_URL} in your Chrome; waiting for remote debugging to be switched on...")
        while ws_url is None:
            if time.time() >= deadline:
                return _result(
                    "needs_remote_debugging",
                    f"Open {INSPECT_URL} in your Chrome (a tab was opened), switch remote "
                    "debugging ON, then try again.",
                )
            time.sleep(1)
            ws_url = _everyday_chrome_ws_url(say)

    say("Connecting to Chrome... if it asks whether to allow remote debugging, click Allow.")
    try:
        # No Origin header: everyday Chrome rejects browser-origin connections.
        conn = CDPConnection(ws_url, origin=None, timeout=max(30.0, min(120.0, timeout)))
    except Exception as exc:  # noqa: BLE001
        return _result(
            "error",
            f"Could not connect to Chrome ({type(exc).__name__}: {exc}). Make sure remote "
            f"debugging is on at {INSPECT_URL} and that you clicked Allow, then try again.",
        )

    with conn:
        login_tab_opened = False
        while True:
            try:
                cookies = _facebook_only(conn.call("Storage.getCookies").get("cookies", []))
            except Exception as exc:  # noqa: BLE001
                return _result("error", f"Could not read cookies from Chrome: {exc}")
            if _logged_in(cookies):
                version = None
                try:
                    version = conn.call("Browser.getVersion")
                except Exception as exc:  # noqa: BLE001
                    say(f"(Could not read the browser's User-Agent: {exc})")
                return _saved(
                    cookies, version, say,
                    f"You can switch remote debugging back OFF at {INSPECT_URL}.",
                )
            if not login_tab_opened:
                try:
                    conn.call("Target.createTarget", {"url": LOGIN_URL})
                    say("Your Chrome isn't logged in to Facebook; opened a login tab there.")
                except Exception as exc:  # noqa: BLE001
                    say(f"(Could not open a login tab: {exc})")
                login_tab_opened = True
            if time.time() >= deadline:
                return _result(
                    "waiting",
                    "Your Chrome isn't logged in to Facebook. Log in on the Facebook tab "
                    "that was opened there, then try again.",
                )
            time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    if "--default-chrome" in sys.argv:
        print("== Facebook cookie capture from your everyday Chrome ==")
        result = capture_from_everyday_chrome(LOGIN_TIMEOUT, say=print)
    else:
        print("== Facebook cookie capture (Chrome login window) ==")
        result = capture_with_login_window(LOGIN_TIMEOUT, force_open="--open" in sys.argv, say=print)

    print("\n" + result["message"])
    return {"saved": 0, "waiting": 2, "needs_remote_debugging": 2}.get(result["status"], 3)


if __name__ == "__main__":
    sys.exit(main())

"""A small headless-Chrome driver, for the browser suite only.

Talks the DevTools protocol over a websocket rather than pulling in Selenium or
Playwright: the front end has no build step and no runtime dependencies, and the
tests that drive it should not add a browser download to `pip install`.

Everything the tests need comes down to `evaluate()`. Chrome's evaluate is a
debugger command, so the app's own Content-Security-Policy does not block it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

FLAGS = (
    "--headless=new",
    "--disable-gpu",
    "--no-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--window-size=1500,1000",
    "--remote-debugging-port=0",
)


def find_chrome() -> str | None:
    """The first Chrome or Edge we can find, or None if there is none."""
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chrome")


class BrowserError(RuntimeError):
    """A DevToolsprotocol call failed, or a wait ran out of time."""


class Browser:
    """One headless browser tab, driven by evaluating JavaScript in it."""

    def __init__(self, binary: str, profile: Path, timeout: float = 20.0) -> None:
        from websockets.sync.client import connect

        self.timeout = timeout
        self.profile = profile
        self.process = subprocess.Popen(
            [binary, *FLAGS, f"--user-data-dir={profile}", "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port = self._wait_for_port()
        self.socket = connect(self._page_socket(port), open_timeout=timeout, max_size=None)
        self._next_id = 0

    # -- start-up ---------------------------------------------------------- #

    def _wait_for_port(self) -> int:
        """Chrome writes the port it chose into the profile directory."""
        marker = self.profile / "DevToolsActivePort"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                # Windows can refuse the read while Chrome still holds the file.
                text = marker.read_text(encoding="utf-8").splitlines()
            except OSError:
                text = []
            if text and text[0].strip().isdigit():
                return int(text[0].strip())
            if self.process.poll() is not None:
                raise BrowserError("the browser exited before it was ready")
            time.sleep(0.05)
        raise BrowserError("the browser never reported a debugging port")

    def _page_socket(self, port: int) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=5
                ) as feed:
                    targets = json.load(feed)
            except OSError:
                time.sleep(0.1)
                continue
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
            time.sleep(0.1)
        raise BrowserError("no page target to attach to")

    # -- the protocol ------------------------------------------------------ #

    def send(self, method: str, **params: Any) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self.socket.send(json.dumps({"id": message_id, "method": method, "params": params}))

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv(timeout=self.timeout))
            if message.get("id") != message_id:
                continue  # an event we did not ask for
            if "error" in message:
                raise BrowserError(f"{method}: {message['error']}")
            return message.get("result", {})
        raise BrowserError(f"{method}: no reply")

    def evaluate(self, expression: str) -> Any:
        """Run JavaScript in the page and return its value."""
        result = self.send(
            "Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True
        )
        if "exceptionDetails" in result:
            thrown = result["exceptionDetails"].get("exception", {})
            raise BrowserError(thrown.get("description") or result["exceptionDetails"])
        return result.get("result", {}).get("value")

    # -- the bits the tests use -------------------------------------------- #

    def goto(self, url: str, ready: bool = True) -> None:
        """Load a page.

        `ready` waits for the app to have rendered, which is what a test wants.
        A caller clearing storage before the app can read it does not want to
        wait for a render that may not happen: the app restores whichever tab
        was last open, and the Compare tab has no composer in it.
        """
        self.send("Page.navigate", url=url)
        self.wait_for("document.readyState === 'complete'")
        if ready:
            # app.js renders on load; wait for it to have put something on screen.
            self.wait_for("!!document.querySelector('#advisor-view .composer')")

    def wait_for(self, predicate: str, timeout: float = 10.0) -> None:
        """Poll a JavaScript predicate until it is true."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = self.evaluate(f"!!({predicate})")
            except BrowserError as error:
                last = str(error)
            if last is True:
                return
            time.sleep(0.05)
        # A timeout here is usually a symptom rather than the fault, so take a
        # reading of the app's own state on the way out: whether it thinks a
        # reply is in flight, and whether it put anything on screen about it.
        detail = ""
        with contextlib.suppress(BrowserError):  # the page is gone, answer enough
            detail = self.evaluate(
                "JSON.stringify({loading: state.loading,"
                " alert: state.alert && state.alert.kind,"
                " replies: document.querySelectorAll('.reply').length,"
                " toast: (document.getElementById('toasts') || {}).textContent})"
            )
        raise BrowserError(f"waiting for `{predicate}` timed out (last: {last!r}) {detail}")

    def text(self, selector: str) -> str:
        return self.evaluate(f"(document.querySelector({selector!r}) || {{}}).textContent || ''")

    def count(self, selector: str) -> int:
        return self.evaluate(f"document.querySelectorAll({selector!r}).length")

    def exists(self, selector: str) -> bool:
        return bool(self.evaluate(f"!!document.querySelector({selector!r})"))

    def click(self, selector: str) -> None:
        clicked = self.evaluate(
            f"(() => {{ const node = document.querySelector({selector!r});"
            " if (!node) return false; node.click(); return true; })()"
        )
        if not clicked:
            raise BrowserError(f"nothing to click at {selector}")

    def fill(self, selector: str, value: str) -> None:
        filled = self.evaluate(
            f"(() => {{ const node = document.querySelector({selector!r});"
            f" if (!node) return false; node.value = {value!r};"
            " node.dispatchEvent(new Event('input', {bubbles: true})); return true; })()"
        )
        if not filled:
            raise BrowserError(f"nothing to fill at {selector}")

    def ask(self, question: str) -> None:
        """Type a question into the composer, send it, and see it leave.

        Waiting for the app to go busy is what stops a test racing ahead of the
        request it just made: without it, a "wait until it settles" check can
        pass against the state from before the click, and everything after it
        measures the wrong turn.
        """
        self.fill("#composer-input", question)
        self.click("#send-btn")
        self.wait_for("document.body.classList.contains('is-busy')")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.socket.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

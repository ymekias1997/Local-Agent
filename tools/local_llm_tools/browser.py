"""Small Chromium browser driver using the DevTools protocol over private pipes.

A fresh profile prevents the agent from inheriting the user's browser sessions.
CDP's download policy denies automatic file downloads; approved download_file is
the only supported download route. There is intentionally no arbitrary JavaScript
execution tool. Navigation and UI interactions all require an exact approval.
"""
from __future__ import annotations

import atexit
import base64
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from .capabilities import require_approval
from .registry import string


class BrowserTools:
    def __init__(self, state_dir, approve):
        self.directory = Path(state_dir) / "browser" / uuid.uuid4().hex
        self.approve = approve
        self.process = None
        self.counter = 0
        self.buffer = b""
        self.session = None
        self.url = "about:blank"

    def _start(self):
        if self.process is not None:
            if self.process.poll() is not None:
                raise RuntimeError("Browser process exited; create a new agent session")
            return
        executable = shutil.which("chromium") or shutil.which("chromium-browser")
        if not executable:
            raise RuntimeError("Chromium is missing; install it only with user approval")
        self.directory.mkdir(parents=True, mode=0o700)
        child_read, self.write_fd = os.pipe()
        self.read_fd, child_write = os.pipe()
        # CDP requires descriptors 3 and 4. A tiny exec wrapper remaps inherited
        # descriptors without preexec_fn, which is unsafe in multithreaded apps.
        wrapper = ("import os,sys; r=os.dup(int(sys.argv[1])); w=os.dup(int(sys.argv[2])); "
                   "os.dup2(r,3); os.dup2(w,4); os.set_inheritable(3,True); os.set_inheritable(4,True); "
                   "os.execv(sys.argv[3],sys.argv[3:])")
        try:
            self.process = subprocess.Popen([sys.executable, "-c", wrapper, str(child_read), str(child_write), executable,
                "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check",
                "--disable-background-networking", "--disable-sync", "--disable-extensions",
                "--disable-component-update", "--disable-default-apps", "--disable-breakpad",
                "--user-data-dir=" + str(self.directory / "profile"), "about:blank"],
                pass_fds=(child_read, child_write), stdin=subprocess.DEVNULL, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            os.close(self.read_fd)
            os.close(self.write_fd)
            raise
        finally:
            os.close(child_read)
            os.close(child_write)
        atexit.register(self.close)
        try:
            self._call("Browser.setDownloadBehavior", {"behavior": "deny"}, browser=True)
            targets = self._call("Target.getTargets", browser=True)["targetInfos"]
            target = next(item["targetId"] for item in targets if item["type"] == "page")
            self.session = self._call("Target.attachToTarget", {"targetId": target, "flatten": True}, browser=True)["sessionId"]
            self._call("Page.enable")
        except Exception:
            self.close()
            raise

    def _call(self, method, params=None, browser=False):
        self.counter += 1
        identifier = self.counter
        request = {"id": identifier, "method": method, "params": params or {}}
        if self.session and not browser:
            request["sessionId"] = self.session
        raw = json.dumps(request).encode() + b"\0"
        while raw:
            written = os.write(self.write_fd, raw)
            raw = raw[written:]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\0" in self.buffer:
                message, self.buffer = self.buffer.split(b"\0", 1)
                value = json.loads(message)
                if value.get("id") == identifier:
                    if "error" in value:
                        raise RuntimeError(str(value["error"]))
                    return value.get("result", {})
            if select.select([self.read_fd], [], [], max(0, deadline - time.monotonic()))[0]:
                chunk = os.read(self.read_fd, 65536)
                if not chunk:
                    raise RuntimeError("Chromium closed its control pipe")
                self.buffer += chunk
                if len(self.buffer) > 20_000_000:
                    raise RuntimeError("Browser response exceeds 20 MB")
        raise TimeoutError(f"Chromium timed out during {method}")

    def _evaluate(self, expression):
        response = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if "exceptionDetails" in response:
            raise RuntimeError("Browser element operation failed: " + str(response["exceptionDetails"])[:1000])
        return response.get("result", {}).get("value")

    def browser_navigate(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Browser navigation requires an HTTP(S) URL without embedded credentials")
        require_approval(self.approve, "browser_navigate", {"url": url,
                         "effect": "Open this URL in an isolated browser; page scripts and redirects may contact other sites; file downloads are denied"})
        self._start()
        result = self._call("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise RuntimeError(result["errorText"])
        self.url = url
        return {"url": url, "navigation": result, "note": "Navigation started; use browser_snapshot to inspect the loaded page"}

    def browser_snapshot(self):
        self._start()
        value = self._evaluate("JSON.stringify({url:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,20000),elements:Array.from(document.querySelectorAll('a,button,input,textarea,select')).slice(0,150).map(e=>({tag:e.tagName,id:e.id,name:e.name,text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,200),type:e.type,href:e.href}))})")
        result = json.loads(value)
        self.url = result["url"]
        return result

    def _require_page(self):
        # Restored jobs must navigate explicitly; an expired browser is not the
        # same target that an operator previously inspected and approved.
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("No live browser session; navigate to the target page first")

    def _page_url(self):
        return self._evaluate("location.href")

    def browser_click(self, selector: str):
        self._require_page()
        approved_url = self._page_url()
        require_approval(self.approve, "browser_click", {"url": approved_url, "selector": selector,
                         "effect": "Click the matching element; may submit a form, send a message, spend money or change an account"})
        if self._page_url() != approved_url:
            raise PermissionError("Browser page changed during approval; inspect it again")
        return {"clicked": self._evaluate("(()=>{const e=document.querySelector(" + json.dumps(selector) + ");if(!e)throw Error('Element not found');e.click();return true})()")}

    def browser_type(self, selector: str, text: str):
        self._require_page()
        approved_url = self._page_url()
        require_approval(self.approve, "browser_type", {"url": approved_url, "selector": selector, "text": text,
                         "effect": "Replace the element's value and send input/change events; the site may submit data automatically"})
        if self._page_url() != approved_url:
            raise PermissionError("Browser page changed during approval; inspect it again")
        expression = "(()=>{const e=document.querySelector(" + json.dumps(selector) + ");if(!e)throw Error('Element not found');e.focus();e.value=" + json.dumps(text) + ";e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));return true})()"
        return {"typed": self._evaluate(expression)}

    def browser_screenshot(self):
        self._start()
        result = self._call("Page.captureScreenshot", {"format": "png"})
        path = self.directory / f"screenshot-{uuid.uuid4().hex}.png"
        path.write_bytes(base64.b64decode(result["data"], validate=True))
        return {"path": str(path), "image_path": str(path)}

    def close(self):
        if self.process is not None:
            # Ask Chromium to flush its profile and stop renderer descendants
            # gracefully. Killing only the browser process leaves children writing
            # profile files, which races cleanup and leaks resource usage.
            try:
                os.write(self.write_fd, json.dumps({"id": self.counter + 1, "method": "Browser.close"}).encode() + b"\0")
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(self.read_fd)
            os.close(self.write_fd)
            self.process = None
        return {"closed": True}

    def register(self, registry):
        registry.add(self.browser_navigate, "Open a URL in an isolated Chromium browser after specific approval; all file downloads are denied.", {"url": string(minLength=1, maxLength=8000)}, ["url"])
        registry.add(self.browser_snapshot, "Read current browser page text and up to 150 interactive element descriptions.", {})
        registry.add(self.browser_click, "Click a CSS selector after specific approval; may have external effects.", {"selector": string(minLength=1, maxLength=2000)}, ["selector"])
        registry.add(self.browser_type, "Fill a CSS selector after specific approval; triggers page input/change events.", {"selector": string(minLength=1, maxLength=2000), "text": string(maxLength=10000)}, ["selector", "text"])
        registry.add(self.browser_screenshot, "Save a PNG of the browser page to local artifacts.", {})
        registry.add(self.close, "Close this agent's isolated browser.", {}, name="browser_close")

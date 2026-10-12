"""Launch dedicated ordinary Chrome, then attach Playwright over loopback CDP."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import socket
from urllib.request import build_opener, ProxyHandler

from dealforge.providers.cvs.account_sync import HOME, SyncProblem


def chrome_executable():
    if os.name == "nt":
        roots = (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA"))
        candidates = [Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots if root]
    elif os.sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    else:
        candidates = [Path(path) for name in ("google-chrome", "google-chrome-stable") if (path := shutil.which(name))]
    for path in candidates:
        if path.is_file():
            return path
    raise SyncProblem("browser_unavailable", "native_chrome_not_installed", "Install Google Chrome to use the native_cdp launch mode.")


def native_arguments(profile, port, *, headless=False, startup_url=HOME):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Use an allocated nonzero loopback port")
    if startup_url not in {HOME, "about:blank"}:
        raise ValueError("Use the CVS home page or an empty test page")
    return [f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
        *(["--headless=new"] if headless else []), startup_url]


def _ready(endpoint):
    # Local metadata only; never inherit a proxy or persist the debugger URL.
    with build_opener(ProxyHandler({})).open(endpoint + "/json/version", timeout=1) as response:
        data = json.loads(response.read(65536))
    return isinstance(data, dict) and str(data.get("Browser", "")).startswith("Chrome/")


@asynccontextmanager
async def native_context(playwright, settings, *, startup_url=HOME):
    if settings.channel != "chrome":
        raise ValueError("native_cdp requires the installed Chrome channel")
    executable = chrome_executable()
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(str(executable),
        *native_arguments(settings.profile, port, headless=settings.headless, startup_url=startup_url),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    browser = None
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                break
            try:
                if await asyncio.to_thread(_ready, endpoint):
                    browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=10000)
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        if browser is None or len(browser.contexts) != 1:
            raise SyncProblem("browser_unavailable", "native_browser_connection_failed",
                "Close the other dedicated CVS browser, then retry. Your normal Chrome window can stay open.")
        yield browser.contexts[0]
    finally:
        if browser is not None:
            try:
                # Graceful shutdown flushes this task's own profile. A CDP
                # disconnect alone would leave the externally launched browser.
                connection = await browser.new_browser_cdp_session()
                await connection.send("Browser.close")
            except Exception:
                pass
            await browser.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()

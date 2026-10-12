"""Select a CVS collection transport without changing account/tool contracts."""

from contextlib import asynccontextmanager
import os

from dealforge.providers.cvs.account_sync import SyncProblem


def browser_backend():
    backend = os.environ.get("DEALFORGE_CVS_BROWSER", "chrome_mcp")
    if backend not in {"chrome_mcp", "playwright"}:
        raise SyncProblem("invalid_input", "invalid_browser_backend",
            "Set DEALFORGE_CVS_BROWSER to chrome_mcp or playwright.")
    return backend


@asynccontextmanager
async def configured_collector(*, chrome_factory=None):
    backend = browser_backend()
    if backend == "playwright":
        from dealforge.providers.cvs.playwright_browser import playwright_collector
        factory = playwright_collector
    else:
        from dealforge.providers.cvs.account_sync import chrome_collector
        factory = chrome_factory or chrome_collector
    try:
        async with factory() as collector:
            yield collector
    except ValueError:
        raise SyncProblem("invalid_input", "invalid_browser_settings",
            "Use a dedicated profile outside the repo, channel chrome/chromium, and headless true/false.") from None


def connection_advice():
    if os.environ.get("DEALFORGE_CVS_BROWSER") == "playwright":
        return "Run dealforge-cvs login in the dedicated browser; check the CVS extra and browser installation."
    return "Keep Chrome open with debugging allowed, then retry. Check Node/npx is installed."

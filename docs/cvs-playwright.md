# Dedicated Playwright browser for CVS

Implemented and live-verified October 12, 2026. Ordinary Chrome launches with a
dedicated persistent profile, and Playwright attaches directly over loopback CDP.
Interactive sign-in, fresh inventory collection, device continuity and persistence
across browser restart have been verified. Account-specific observations are private.
The project's configured MCP server now uses this backend. Account terms and
checkout behavior remain partial; headless and hosted access are unverified.

## Local setup

Install the project with its CVS extra:

```powershell
python -m pip install -e ".[cvs]"
dealforge-cvs login
```

The login command opens installed Google Chrome normally with a dedicated profile
and connects Playwright directly. The
shopper enters credentials directly in that browser and opens Coupons & Rewards.
Once the inventory appears, the command performs a fresh account sync, closes
the browser, launches a new process with the same profile, and syncs again.
`session_persistence_verified=true` requires a fresh second snapshot and matched
device-local card continuity. It does not verify ownership, full terms or POS
behavior. The command returns a small diagnostic summary, without raw card
numbers, credentials or reward amounts. Informational progress goes to stderr;
the final JSON goes to stdout.

The browser profile defaults to `%LOCALAPPDATA%\DealForge\cvs\playwright-profile`
on Windows, or `$XDG_STATE_HOME/DealForge/cvs/playwright-profile` on other systems.
It contains authenticated browser state and stays outside the repository.
The collector never imports the shopper's normal Chrome profile or cookies.

After successful sign-in, select Playwright per CLI command:

```powershell
dealforge-cvs sync --browser playwright
dealforge-cvs sync --browser playwright --detail-query Colgate --detail-limit 3
dealforge-cvs stores --browser playwright --zip 00000
dealforge-cvs search "Colgate toothpaste" --browser playwright --zip 00000
```

Or select it for a CLI/MCP process through its environment:

```powershell
$env:DEALFORGE_CVS_BROWSER = 'playwright'
dealforge-cvs-mcp
```

The project's configured MCP server already sets `DEALFORGE_CVS_BROWSER=playwright`
and `DEALFORGE_CVS_LAUNCH=native_cdp`. Restart the host session to load those
settings. A shell environment set after the host started may not reach its MCP
subprocess. The eleven tool names and account/basket schemas are unchanged.
Unconfigured CLI/MCP processes still default to the existing `chrome_mcp`
backend; override it with the CLI flag or environment shown above.

## Browser settings

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DEALFORGE_CVS_BROWSER` | `chrome_mcp` | `playwright` selects the dedicated transport; unsupported values fail explicitly |
| `DEALFORGE_CVS_PROFILE` | Dedicated state directory above | Custom absolute profile path outside the checkout; the normal Windows Chrome User Data tree is rejected |
| `DEALFORGE_CVS_CHANNEL` | `chrome` | Installed Google Chrome; `chromium` selects Playwright's bundled browser |
| `DEALFORGE_CVS_HEADLESS` | `false` | `true` for noninteractive collection; the login command always uses a visible browser |
| `DEALFORGE_CVS_LAUNCH` | `native_cdp` for Chrome; `managed` for Chromium | Ordinary installed Chrome plus direct Playwright CDP attachment, or Playwright's managed persistent browser |

The verified native mode launches only the dedicated profile and binds debugging
to loopback on a temporary nonzero port. It does not launch Chrome DevTools MCP,
attach to personal Chrome windows, or require repeated Chrome debugging approval.
Playwright's managed Chrome launch was the mode that failed the real sign-in.

For the bundled browser, run `python -m playwright install chromium` and set
`DEALFORGE_CVS_CHANNEL=chromium`. CVS sign-in has not been tested with that
channel. Headless real-account access and hosted browser access are unverified.
Do not assume that a headed session works from a different machine or network.

An OS lock prevents two DealForge processes from using the same profile at
once. Close the login task before starting a CLI sync or MCP collection using
that profile. Process exit releases the lock; no profile deletion is needed.
The collector never silently falls back to the other backend.

## Collection boundaries

`PlaywrightSession` supplies navigation, observed control references and
accessibility snapshots to the existing account/catalog collectors. Page
responses are observed only for the two allowlisted account/definition routes.
Only successful response bodies are read, with size and retention bounds; they
remain in memory until discarded. No private requests are replayed, no browser
JavaScript is evaluated, and no credentials are entered by the collector.

Normal More details visits, search fields, the store dialog and presentation
scrolling are supported. Coupon activation, cart changes and checkout controls
are refused. Normalized results retain device-local card bindings, visible-count
reconciliation, unknown terms and freshness checks. Catalog compatibility has
synthetic coverage. Personal store and product observations are not published.
Hidden page values must not become observed prices or shelf prices.

## Validation and sign-in investigation

Twenty-four transport tests pass, including installed Chrome integration tests
for both launch modes, with synthetic CVS HTML/profile responses. Each
checks fresh count reconciliation, account continuity across two browser
processes and persistence of a fictional cookie with an explicit expiration.
These tests are separate from the successful real-account restart verification.

All 363 CVS tests pass with the browser integrations enabled, including the new
trip-planning and offer-assessment checks. The full suite has 550 passes and the
same three pre-existing HEB parser failures. Catalog
regressions cover aggregated ZIP-result labels, conflicting ZIP summaries,
late-loading store headers and product-result markers represented as text.

```powershell
$env:DEALFORGE_TEST_PLAYWRIGHT = 'true'
python -m pytest -q tests/test_cvs_playwright.py
```

CVS may reject sign-in in some managed-browser configurations. The ordinary-Chrome
launch mode provides an alternative using a separate persistent profile. Keep
network configuration, sign-in diagnostics and session material private. The
collector does not modify proxies, VPNs, security checks or browser fingerprints.

Design references: [persistent browser contexts](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context),
[authentication state](https://playwright.dev/python/docs/auth),
[accessibility snapshots](https://playwright.dev/python/docs/aria-snapshots),
[response observation](https://playwright.dev/python/docs/network).
See also [Chrome's dedicated-profile requirement for remote debugging](https://developer.chrome.com/blog/remote-debugging-port).

#!/usr/bin/env python3
"""
fetch_page.py - stealth JS-page / share-URL fetcher (UPGRADED).

Keeps our STRONG browser layer unchanged:
  * patchright (stealth-patched Playwright fork) instead of plain Playwright
  * headless=False under xvfb-run (real-looking browser, not headless)
  * prebuilt Docker image (ghcr.io/siddhantanandi2003-debug/fetch:latest)

Adds the robustness checks our old fetch.yml was missing (idea source:
canvas-toolbox issue #52, lib/tools/grader_follow_share_url.py):
  1. share-URL detection + service-specific selectors with fallback
  2. bot-wall detection (google.com/sorry/index) -> FAIL LOUD, never index a captcha
  3. revoked / deleted share detection
  4. meaningful-content threshold (>= MIN_CHARS)
  5. machine-readable fetch_status.json  (OK / BOTWALL / REVOKED / EMPTY / ERROR)

Outputs (same as before + one new file):
  page_text.txt      rendered body/selector text
  page.html          full HTML
  page.json          JSON body if the URL serves JSON
  page.bin           raw bytes of the URL (images/files)
  fetch_status.json  machine-readable verdict  (NEW)

Exit codes:  0 OK | 2 BOTWALL | 3 REVOKED | 4 EMPTY | 5 ERROR
"""
import asyncio
import json
import os
import re
import sys

# --------------------------------------------------------------------------
# share-URL detection  (ported from canvas-toolbox issue #52)
# --------------------------------------------------------------------------
_SHARE_PATTERNS = [
    ("chatgpt", re.compile(r"https?://(?:chatgpt\.com|chat\.openai\.com)/share/([A-Za-z0-9-]+)")),
    ("gemini",  re.compile(r"https?://(?:gemini\.google\.com|bard\.google\.com)/share/([A-Za-z0-9_-]+)")),
    ("aimode",  re.compile(r"https?://share\.google/aimode/([A-Za-z0-9_-]{8,})")),
]


def detect_service(url: str):
    for svc, rx in _SHARE_PATTERNS:
        if rx.search(url):
            return svc
    return None


# selector order per service; first non-empty (>= MIN_CHARS) wins; body is the floor
_SERVICE_SELECTORS = {
    "chatgpt": ["#thread", "main", "body"],
    "gemini":  ["main", "body"],
    "aimode":  ["main", "[role='main']", "body"],
    "generic": ["main", "[role='main']", "article", "body"],
}

MIN_CHARS = 200

REVOKED_MARKERS = (
    "the conversation has been deleted",
    "this share is no longer available",
    "conversation not found",
    "this link has expired",
)

BOTWALL_MARKERS = ("google.com/sorry/", "/sorry/index")

# patchright masks navigator.webdriver itself; this is harmless belt-and-braces
_WEBDRIVER_MASK = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


def _write_status(status: str, detail: str, used_selector: str = "", extra=None):
    payload = {"status": status, "detail": detail, "used_selector": used_selector}
    if extra:
        payload.update(extra)
    with open("fetch_status.json", "w") as f:
        json.dump(payload, f, indent=2)
    print("FETCH_STATUS", status, "|", detail)


async def main() -> int:
    url = os.environ.get("PAGE_URL", "").strip()
    if not url:
        _write_status("ERROR", "PAGE_URL not set")
        return 5

    service = detect_service(url)
    selectors = _SERVICE_SELECTORS.get(service or "generic")
    print("URL", url[:160])
    print("SERVICE", service or "generic")

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        try:
            from playwright.async_api import async_playwright  # graceful fallback
        except ImportError:
            _write_status("ERROR", "neither patchright nor playwright installed")
            return 5

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, channel="chromium")
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
            )
            page = await ctx.new_page()
            await page.add_init_script(_WEBDRIVER_MASK)
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)

            # stability loop: wait until innerText stops growing
            last_len, same = -1, 0
            for i in range(50):
                await page.wait_for_timeout(3000)
                try:
                    txt = await page.evaluate("document.body ? document.body.innerText : ''")
                except Exception as e:  # noqa: BLE001
                    print("tick", i, "err", e)
                    continue
                print("tick", i, "len", len(txt))
                same = same + 1 if len(txt) == last_len else 0
                last_len = len(txt)
                if same >= 2 and len(txt) > 100:
                    break

            # --- 1. BOT-WALL (fail loud, never index the captcha) -----------
            final_url = page.url
            if any(m in final_url for m in BOTWALL_MARKERS):
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                _write_status(
                    "BOTWALL",
                    "Google/other bot-detection wall reached (" + final_url[:120] + "). "
                    "Stealth layer did not suffice on this IP/session. Retry later or "
                    "use the ZenRows premium-proxy route.",
                )
                return 2

            # --- 2. REVOKED / DELETED share ---------------------------------
            try:
                content_lower = (await page.content())[:10000].lower()
            except Exception:  # noqa: BLE001
                content_lower = ""
            if any(m in content_lower for m in REVOKED_MARKERS):
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                _write_status("REVOKED", "share link deleted / no longer available")
                return 3

            # --- 3. selector fallback + meaningful-content threshold --------
            chosen_text, used_selector = "", ""
            for sel in selectors:
                try:
                    text = await page.locator(sel).first.inner_text(timeout=4000)
                except Exception:  # noqa: BLE001
                    continue
                if text and len(text.strip()) >= MIN_CHARS:
                    chosen_text, used_selector = text, sel
                    break
            if not chosen_text:
                try:
                    body = await page.evaluate("document.body ? document.body.innerText : ''")
                except Exception:  # noqa: BLE001
                    body = ""
                if len((body or "").strip()) >= MIN_CHARS:
                    chosen_text, used_selector = body, "body-innerText"
                else:
                    try:
                        await browser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    _write_status(
                        "EMPTY",
                        "no rendered content >= %d chars (possible bot wall or unloaded SPA)"
                        % MIN_CHARS,
                    )
                    return 4

            with open("page_text.txt", "w") as f:
                f.write(chosen_text)
            print("PAGE_TEXT_LEN", len(chosen_text), "SELECTOR", used_selector)

            # --- extras kept from the original fetch.yml --------------------
            try:
                resp = await ctx.request.get(url)
                if resp.status == 200:
                    body_bytes = await resp.body()
                    with open("page.bin", "wb") as f:
                        f.write(body_bytes)
                    print("BINARY_LEN", len(body_bytes))
            except Exception as e:  # noqa: BLE001
                print("binary err", e)

            try:
                r = await page.evaluate(
                    """async (u) => {
                        const resp = await fetch(u, {headers: {'Accept': 'application/json'}});
                        return {status: resp.status, body: await resp.text()};
                    }""",
                    url,
                )
                body = (r or {}).get("body", "")
                if (r or {}).get("status") == 200 and body.strip().startswith("{"):
                    with open("page.json", "w") as f:
                        f.write(body)
                    print("GOT_JSON_LEN", len(body))
            except Exception as e:  # noqa: BLE001
                print("json attempt err", e)

            try:
                html = await page.content()
                with open("page.html", "w") as f:
                    f.write(html)
                print("PAGE_HTML_LEN", len(html))
            except Exception as e:  # noqa: BLE001
                print("html err", e)

            await browser.close()

        _write_status("OK", "content rendered", used_selector=used_selector)
        return 0

    except Exception as e:  # noqa: BLE001
        _write_status("ERROR", "%s: %s" % (type(e).__name__, e))
        return 5


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

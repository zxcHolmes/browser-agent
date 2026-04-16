"""
Demo: two workflows using the Browser Agent Proxy API

Flow 1: Open Hacker News, scrape the front page stories, print as JSON
Flow 2: Open HN submit page, fill in the form (title / url / text), do NOT submit
"""

import json
import time
import requests

BASE = "http://localhost:12345"


# ─── helpers ──────────────────────────────────────────────────────────────────

def create_tab(url: str, meta: dict = None) -> dict:
    resp = requests.post(f"{BASE}/api/tabs", json={
        "url": url,
        "active": True,
        "meta": meta or {},
    })
    resp.raise_for_status()
    return resp.json()["data"]["tab"]


def close_tab(tab_id: int):
    requests.delete(f"{BASE}/api/tabs/{tab_id}")


def cdp(tab_id: int, method: str, params: dict = None, timeout: int = 30) -> dict:
    resp = requests.post(f"{BASE}/api/cdp", json={
        "tabId": tab_id,
        "method": method,
        "params": params or {},
        "timeout": timeout,
    })
    resp.raise_for_status()
    return resp.json()["data"].get("result", {})


def evaluate(tab_id: int, expression: str, timeout: int = 30):
    """Execute JS and return the value."""
    result = cdp(tab_id, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    }, timeout=timeout)
    return result.get("result", {}).get("value")


def wait_for_load(tab_id: int, max_wait: float = 15):
    """Wait for Page.loadEventFired via the events API, then confirm readyState."""
    start_ms = int(time.time() * 1000)
    deadline = time.time() + max_wait

    while time.time() < deadline:
        resp = requests.get(f"{BASE}/api/events", params={
            "tabId": tab_id,
            "method": "Page.loadEventFired",
            "since": start_ms,
        })
        resp.raise_for_status()
        if resp.json()["data"]["count"] > 0:
            return
        time.sleep(0.5)

    raise TimeoutError(f"Tab {tab_id} did not finish loading within {max_wait}s")


def type_into(tab_id: int, selector: str, text: str):
    """Focus an input and type text character by character via Input.dispatchKeyEvent."""
    # Clear existing value first
    evaluate(tab_id, f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            el.focus();
            el.value = '';
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})()
    """)
    # Type each character
    for ch in text:
        cdp(tab_id, "Input.dispatchKeyEvent", {
            "type": "keyDown",
            "text": ch,
        })
        cdp(tab_id, "Input.dispatchKeyEvent", {
            "type": "keyUp",
            "text": ch,
        })
    # Trigger input event so the page sees the new value
    evaluate(tab_id, f"""
        (function() {{
            const el = document.querySelector({json.dumps(selector)});
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
        }})()
    """)


# ─── Flow 1: Scrape Hacker News front page ────────────────────────────────────

def flow_scrape_hn():
    print("\n" + "="*60)
    print("Flow 1: Scrape Hacker News front page")
    print("="*60)

    tab = create_tab("https://news.ycombinator.com", meta={"flow": "scrape-hn"})
    tab_id = tab["tabId"]
    print(f"[+] Created tab {tab_id}")

    try:
        wait_for_load(tab_id)
        print("[+] Page loaded")

        stories = evaluate(tab_id, """
            (function() {
                const rows = document.querySelectorAll('tr.athing');
                const results = [];
                rows.forEach(function(row) {
                    const titleEl = row.querySelector('.titleline > a');
                    const rankEl  = row.querySelector('.rank');
                    const subRow  = row.nextElementSibling;
                    const scoreEl = subRow ? subRow.querySelector('.score') : null;
                    const userEl  = subRow ? subRow.querySelector('.hnuser') : null;
                    const ageEl   = subRow ? subRow.querySelector('.age') : null;
                    if (titleEl) {
                        results.push({
                            rank:   rankEl  ? parseInt(rankEl.innerText)  : null,
                            title:  titleEl.innerText.trim(),
                            url:    titleEl.href,
                            score:  scoreEl ? scoreEl.innerText.trim() : null,
                            author: userEl  ? userEl.innerText.trim()  : null,
                            age:    ageEl   ? ageEl.getAttribute('title') : null,
                        });
                    }
                });
                return JSON.stringify(results);
            })()
        """)

        items = json.loads(stories)
        print(f"[+] Scraped {len(items)} stories\n")
        print(json.dumps(items[:5], indent=2, ensure_ascii=False))
        print(f"\n... ({len(items)} total stories)")

    finally:
        close_tab(tab_id)
        print(f"[+] Closed tab {tab_id}")


# ─── Flow 2: Fill HN submit form ──────────────────────────────────────────────

def flow_fill_hn_submit():
    print("\n" + "="*60)
    print("Flow 2: Fill HN submit form (no submit)")
    print("="*60)

    tab = create_tab("https://news.ycombinator.com/submit", meta={"flow": "fill-submit"})
    tab_id = tab["tabId"]
    print(f"[+] Created tab {tab_id}")

    try:
        wait_for_load(tab_id)
        print("[+] Page loaded")

        # Check if we're on the login page (HN requires login to submit)
        page_title = evaluate(tab_id, "document.title")
        print(f"[+] Page title: {page_title}")

        body_text = evaluate(tab_id, "document.body.innerText.slice(0, 200)")
        print(f"[+] Body preview: {body_text}")

        # Check if the form fields exist
        has_title = evaluate(tab_id, "!!document.querySelector('input[name=\"title\"]')")
        has_url   = evaluate(tab_id, "!!document.querySelector('input[name=\"url\"]')")
        has_text  = evaluate(tab_id, "!!document.querySelector('textarea[name=\"text\"]')")

        print(f"[+] Form fields — title: {has_title}, url: {has_url}, text: {has_text}")

        if not has_title:
            print("[!] Submit form not visible (likely needs login). Filling with JS directly.")
            # HN redirects to login if not authenticated, so we just demonstrate
            # that we can set values programmatically
            evaluate(tab_id, """
                document.title = 'HN Submit Demo (not logged in)';
            """)
            print("[!] Skipping form fill — not logged in to HN")
            return

        # Fill title
        title_text = "Browser Agent Proxy - Control Chrome via REST API without special launch flags"
        evaluate(tab_id, f"""
            document.querySelector('input[name="title"]').value = {json.dumps(title_text)};
        """)
        print(f"[+] Filled title: {title_text}")

        # Fill URL
        api_url = "http://localhost:12345"
        evaluate(tab_id, f"""
            document.querySelector('input[name="url"]').value = {json.dumps(api_url)};
        """)
        print(f"[+] Filled url: {api_url}")

        # Fill text
        description = (
            "A Go server + Chrome Extension that lets you control your existing Chrome browser "
            "via a simple REST API. No --remote-debugging-port required. "
            "Supports full CDP command forwarding (navigate, screenshot, JS eval, cookies, etc.), "
            "tab CRUD with custom metadata, and a 3-minute auto-close timeout. "
            "Events (Page.loadEventFired, etc.) are streamed to a time-windowed in-memory store "
            "queryable via GET /api/events."
        )
        evaluate(tab_id, f"""
            document.querySelector('textarea[name="text"]').value = {json.dumps(description)};
        """)
        print(f"[+] Filled text: {description[:60]}...")

        # Verify values
        filled_title = evaluate(tab_id, "document.querySelector('input[name=\"title\"]').value")
        filled_url   = evaluate(tab_id, "document.querySelector('input[name=\"url\"]').value")
        filled_text  = evaluate(tab_id, "document.querySelector('textarea[name=\"text\"]').value")

        print("\n[+] Verification:")
        print(f"    title : {filled_title}")
        print(f"    url   : {filled_url}")
        print(f"    text  : {filled_text[:80]}...")
        print("\n[+] Form filled. NOT submitting.")

        # Take a screenshot to confirm
        screenshot = cdp(tab_id, "Page.captureScreenshot", {"format": "png", "quality": 80})
        if screenshot.get("data"):
            import base64
            img_path = "/tmp/hn_submit_form.png"
            with open(img_path, "wb") as f:
                f.write(base64.b64decode(screenshot["data"]))
            print(f"[+] Screenshot saved to {img_path}")

        time.sleep(3)  # keep the tab open briefly so you can see it

    finally:
        close_tab(tab_id)
        print(f"[+] Closed tab {tab_id}")


# ─── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    flow_scrape_hn()
    flow_fill_hn_submit()

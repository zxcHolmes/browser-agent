"""
Browser Agent — powered by OpenRouter (OpenAI-compatible) tool calling.

Usage:
    python agent.py "Open Hacker News and return the top 5 story titles"
    python agent.py --model qwen/qwen3-235b-a22b "Search Google for Python tutorials"
    python agent.py -q "Go to example.com and return the page title"

Env vars (or .env file):
    OPENROUTER_URL      default: https://openrouter.ai/api/v1/chat/completions
    OPENROUTER_API_KEY  required
    OPENROUTER_MODEL    default: google/gemini-2.5-flash-preview
"""

import argparse
import base64
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Logger ────────────────────────────────────────────────────────────────────

LOG_PATH = os.path.join(os.path.dirname(__file__), "agent.log")

_handler = RotatingFileHandler(
    LOG_PATH, maxBytes=1 * 1024 * 1024, backupCount=2, encoding="utf-8"
)
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
)

log = logging.getLogger("agent")
log.setLevel(logging.DEBUG)
log.addHandler(_handler)

OPENROUTER_URL = os.getenv(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-preview")

BASE = "http://localhost:12345"

# ─── Browser API helpers ───────────────────────────────────────────────────────


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{BASE}{path}", json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{BASE}{path}", params=params or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    r = requests.delete(f"{BASE}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


# ─── Tool implementations ──────────────────────────────────────────────────────


def tool_create_tab(
    url: str = "about:blank", active: bool = False, meta: dict = None
) -> dict:
    """Create a new managed Chrome tab."""
    data = _post("/api/tabs", {"url": url, "active": active, "meta": meta or {}})
    return data["data"]["tab"]


def tool_list_tabs() -> list:
    """List all managed Chrome tabs."""
    data = _get("/api/tabs")
    return data["data"]["tabs"]


def tool_get_tab(tab_id: int) -> dict:
    """Get info about a specific tab."""
    data = _get(f"/api/tabs/{tab_id}")
    return data["data"]["tab"]


def tool_close_tab(tab_id: int) -> dict:
    """Close a managed Chrome tab."""
    data = _delete(f"/api/tabs/{tab_id}")
    return data["data"]


def tool_navigate(
    tab_id: int, url: str, wait_for_load: bool = True, timeout_seconds: int = 30
) -> dict:
    """Navigate a tab to a URL, optionally waiting for the page to finish loading."""
    data = _post(
        f"/api/tabs/{tab_id}/navigate",
        {
            "url": url,
            "waitForLoad": wait_for_load,
            "timeoutSeconds": timeout_seconds,
        },
    )
    return data["data"]


def tool_eval(
    tab_id: int, expression: str, await_promise: bool = False, timeout_seconds: int = 30
) -> Any:
    """Execute JavaScript in a tab and return the result value."""
    data = _post(
        f"/api/tabs/{tab_id}/eval",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "timeoutSeconds": timeout_seconds,
        },
    )
    return data["data"].get("value")


def tool_screenshot(tab_id: int, format: str = "png", quality: int = 80) -> dict:
    """Capture a screenshot of the tab. Saves to /tmp and returns the file path."""
    import tempfile

    r = requests.get(
        f"{BASE}/api/tabs/{tab_id}/screenshot",
        params={"format": format, "quality": quality},
        timeout=60,
    )
    r.raise_for_status()
    ext = "jpg" if format == "jpeg" else "png"
    fd, path = tempfile.mkstemp(prefix=f"tab{tab_id}_", suffix=f".{ext}")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return {"path": path, "size_bytes": len(r.content)}


def tool_read_image(path: str) -> dict:
    """
    Load an image file and inject it into the conversation as a vision message.
    Call this after screenshot() to let the model actually see the image.
    Returns confirmation — the image is appended to the conversation automatically.
    """
    # Actual injection happens in dispatch_tool where messages is accessible.
    # This function just validates the path exists.
    if not os.path.exists(path):
        raise FileNotFoundError(f"Image not found: {path}")
    size = os.path.getsize(path)
    return {"path": path, "size_bytes": size}


def tool_cdp(tab_id: int, method: str, params: dict = None, timeout: int = 30) -> dict:
    """Send any Chrome DevTools Protocol command to a tab."""
    data = _post(
        "/api/cdp",
        {
            "tabId": tab_id,
            "method": method,
            "params": params or {},
            "timeout": timeout,
        },
    )
    return data["data"].get("result", {})


def tool_get_events(tab_id: int = None, since: int = None, method: str = None) -> list:
    """Query browser events (Page.loadEventFired, tab.closed, Network.*, etc.)."""
    params = {}
    if tab_id is not None:
        params["tabId"] = tab_id
    if since is not None:
        params["since"] = since
    if method is not None:
        params["method"] = method
    data = _get("/api/events", params)
    return data["data"]["events"]


# ─── Tool registry ─────────────────────────────────────────────────────────────

TOOL_FUNCTIONS = {
    "create_tab": tool_create_tab,
    "list_tabs": tool_list_tabs,
    "get_tab": tool_get_tab,
    "close_tab": tool_close_tab,
    "navigate": tool_navigate,
    "eval": tool_eval,
    "screenshot": tool_screenshot,
    "read_image": tool_read_image,
    "cdp": tool_cdp,
    "get_events": tool_get_events,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_tab",
            "description": "Create a new managed Chrome tab. Returns tab info including tabId.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to open (default: about:blank)",
                    },
                    "active": {
                        "type": "boolean",
                        "description": "Bring tab to foreground (default: false)",
                    },
                    "meta": {
                        "type": "object",
                        "description": "Arbitrary metadata to attach to the tab",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tabs",
            "description": "List all managed Chrome tabs (only tabs created via this API).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tab",
            "description": "Get detailed info about a specific managed tab.",
            "parameters": {
                "type": "object",
                "required": ["tab_id"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_tab",
            "description": "Close a managed Chrome tab.",
            "parameters": {
                "type": "object",
                "required": ["tab_id"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID to close"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate a tab to a URL, waiting for the page to finish loading.",
            "parameters": {
                "type": "object",
                "required": ["tab_id", "url"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID"},
                    "url": {"type": "string", "description": "URL to navigate to"},
                    "wait_for_load": {
                        "type": "boolean",
                        "description": "Wait for page load event (default: true)",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "eval",
            "description": (
                "Execute JavaScript in a Chrome tab and return the result. "
                "Use this to read page content, click elements, fill forms, etc."
            ),
            "parameters": {
                "type": "object",
                "required": ["tab_id", "expression"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID"},
                    "expression": {
                        "type": "string",
                        "description": "JavaScript expression to evaluate",
                    },
                    "await_promise": {
                        "type": "boolean",
                        "description": "Await returned Promise (default: false)",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Capture a screenshot of a tab. Saves the image to a temp file and returns its path. "
                "To actually view the image, call read_image(path) afterwards."
            ),
            "parameters": {
                "type": "object",
                "required": ["tab_id"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID"},
                    "format": {
                        "type": "string",
                        "description": "Image format: png or jpeg (default: png)",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "JPEG quality 1-100 (default: 80)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": (
                "Inject a local image file into the conversation so you can see it. "
                "Always call this after screenshot() when you need to visually inspect the page."
            ),
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the image file (returned by screenshot)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cdp",
            "description": (
                "Send any Chrome DevTools Protocol command to a tab. "
                "Use this for advanced operations: Input.dispatchKeyEvent, Network.getCookies, etc."
            ),
            "parameters": {
                "type": "object",
                "required": ["tab_id", "method"],
                "properties": {
                    "tab_id": {"type": "integer", "description": "Tab ID"},
                    "method": {
                        "type": "string",
                        "description": "CDP method name, e.g. Input.dispatchKeyEvent",
                    },
                    "params": {
                        "type": "object",
                        "description": "CDP method parameters",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default: 30)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": (
                "Query browser events stored in the relay server. "
                "Events include Page.loadEventFired, Network.*, tab.closed, debugger.detached, etc. "
                "Use since (Unix ms) to poll for new events incrementally."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer", "description": "Filter by tab ID"},
                    "since": {
                        "type": "integer",
                        "description": "Return only events after this Unix ms timestamp",
                    },
                    "method": {
                        "type": "string",
                        "description": "Filter by event method, e.g. Page.loadEventFired",
                    },
                },
            },
        },
    },
]

SYSTEM_PROMPT = """You are a browser automation agent. You control a Chrome browser via the Browser Agent Proxy API.

Available tools:
- create_tab: Open a new Chrome tab (returns tabId)
- list_tabs / get_tab: Inspect managed tabs
- close_tab: Close a tab when done
- navigate: Go to a URL and wait for page load
- eval: Run JavaScript in the page (read DOM, click, fill forms)
- screenshot: Capture a screenshot, saves to file, returns path
- read_image: Load an image file into the conversation so you can see it (always call after screenshot)
- cdp: Send any raw CDP command (e.g. Input.insertText to type into focused element)
- get_events: Poll browser events (Page.loadEventFired, Network.*, etc.)

## Standard operating procedure

1. Always create a tab first, then navigate or use its tabId.
2. Before interacting with any element, enumerate actual DOM elements — never guess selectors.
   - List all buttons: `Array.from(document.querySelectorAll('button')).map(b => ({text: b.innerText.trim(), type: b.type, disabled: b.disabled}))`
   - List all inputs/textareas: `Array.from(document.querySelectorAll('input,textarea')).map(e => ({tag: e.tagName, name: e.name, placeholder: e.placeholder, id: e.id}))`
3. To fill inputs in React/SPA pages, use cdp `Input.insertText` (not innerHTML/innerText):
   - Click/focus the element with eval first, then call cdp `Input.insertText` with the text.
4. After every submit/navigation action, verify with `eval('window.location.href')` that the URL changed.
5. After finishing, close any tabs you opened.
6. If a tab returns 404 TAB_NOT_FOUND, recreate it.

## Stuck detection — MANDATORY rules

- If you perform the same action 2 times in a row with the same result (URL unchanged, same error, null return), you MUST stop and diagnose before retrying.
- Diagnosis steps:
  1. Take a screenshot + read_image to see the current page state visually.
  2. Enumerate all interactive elements (buttons, inputs, links) with their actual text/attributes.
  3. Check `window.location.href` to confirm which page you are on.
  4. Identify the correct target element from the enumeration result, then act on it.
- Never repeat a failing click more than 2 times without diagnosing first.
- If a button click does not cause a URL change within 1 retry, enumerate all buttons and find the correct one.
"""


# ─── Context window management ────────────────────────────────────────────────

MAX_TOKENS = 50_000
# Rough estimate: 1 token ≈ 4 chars
CHARS_PER_TOKEN = 2


IMAGE_TOKEN_FIXED = 512  # treat each image_url block as this many tokens
IMAGE_CHARS_FIXED = IMAGE_TOKEN_FIXED * CHARS_PER_TOKEN


def _msg_chars(msg: dict) -> int:
    """Estimate character count of a message (used to approximate token count)."""
    total = len(msg.get("role", ""))
    content = msg.get("content") or ""
    if isinstance(content, str):
        total += len(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image_url":
                    total += IMAGE_CHARS_FIXED  # fixed cost per image
                else:
                    total += len(str(part))
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        total += len(json.dumps(tc))
    return total


def _total_chars(messages: list) -> int:
    return sum(_msg_chars(m) for m in messages)


def trim_messages(messages: list, max_tokens: int = MAX_TOKENS) -> list:
    """
    Trim messages to stay within max_tokens budget.

    Rules:
    1. Always keep messages[0] (system) and messages[1] (first user prompt).
    2. Middle messages are grouped into atomic units that must not be split:
         - A plain assistant message (no tool_calls) is a unit of 1.
         - An assistant message WITH tool_calls + all its following tool results
           form one atomic unit (N+1 messages).
    3. Drop oldest units first (from the front of the middle section).
    4. Never drop a tool result without its paired assistant(tool_calls).
    """
    max_chars = max_tokens * CHARS_PER_TOKEN

    if len(messages) <= 2:
        return messages

    pinned = messages[:2]  # system + first user: always kept
    tail = messages[2:]  # everything else

    # Group tail into atomic units
    units: list[list[dict]] = []
    i = 0
    while i < len(tail):
        msg = tail[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Collect all immediately following tool results
            group = [msg]
            j = i + 1
            while j < len(tail) and tail[j].get("role") == "tool":
                group.append(tail[j])
                j += 1
            units.append(group)
            i = j
        else:
            units.append([msg])
            i += 1

    # Drop oldest units until we fit
    pinned_chars = _total_chars(pinned)
    while units:
        candidate = pinned + [m for u in units for m in u]
        if _total_chars(candidate) <= max_chars:
            break
        units.pop(0)  # drop oldest unit

    result = pinned + [m for u in units for m in u]
    return result


# ─── Agent loop ────────────────────────────────────────────────────────────────


def dispatch_tool(name: str, args: dict, messages: list) -> str:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool: {name}"})
    try:
        result = fn(**args)

        # read_image: load file and append a user vision message into the conversation
        if name == "read_image":
            path = args.get("path", "")
            ext = path.rsplit(".", 1)[-1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
                ext, "image/png"
            )
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"[Image from {path}]"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        },
                    ],
                }
            )
            return json.dumps({"ok": True, "path": path, "injected": True})

        return json.dumps(result, ensure_ascii=False, default=str)
    except requests.HTTPError as e:
        body = {}
        try:
            body = e.response.json()
        except Exception:
            pass
        return json.dumps({"error": str(e), "detail": body})
    except Exception as e:
        return json.dumps({"error": str(e)})


def chat_completion(messages: list, model: str) -> tuple[dict, dict]:
    """Call OpenRouter chat completions API. Returns (message, usage)."""
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "temperature": 0.3,
            "reasoning": {"effort": "none", "enabled": False, "exclude": True},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"], data.get("usage", {})


DONE_MARKER = "TASK_COMPLETE"
CONFIRM_PROMPT = (
    "Please confirm whether all steps of the task have been completed. "
    "If completed, reply starting with 'TASK_COMPLETE' followed by a brief summary. "
    "If there are still unfinished steps, continue using tools to complete them."
)


def run_agent(
    prompt: str,
    model: str = OPENROUTER_MODEL,
    verbose: bool = True,
    auto_confirm: bool = False,
    max_confirms: int = 3,
) -> str:
    """
    Run the browser agent until the model stops calling tools.

    Args:
        auto_confirm: When True, if the model stops without tool calls and the
                      response doesn't contain DONE_MARKER, automatically append
                      a confirmation prompt and continue until DONE_MARKER is
                      found or max_confirms is exhausted.
        max_confirms: Max number of confirmation re-prompts before giving up.

    Returns the final assistant message.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    log.info(
        "=== agent start | model=%s | auto_confirm=%s | prompt=%s",
        model,
        auto_confirm,
        prompt,
    )
    if verbose:
        print(f"\n[agent] model={model}  auto_confirm={auto_confirm}")
        print(f"[agent] prompt: {prompt}\n")

    iteration = 0
    confirms = 0
    while True:
        iteration += 1

        # Trim context to stay within token budget before each call
        before = len(messages)
        messages = trim_messages(messages)
        dropped = before - len(messages)
        token_est = _total_chars(messages) // CHARS_PER_TOKEN

        if dropped:
            log.warning(
                "context trimmed: dropped %d messages, est=%d tokens",
                dropped,
                token_est,
            )
            if verbose:
                print(
                    f"[agent] context trimmed: dropped {dropped} messages, ~{token_est} tokens remaining"
                )

        log.info("--- iteration %d | est=%d tokens", iteration, token_est)
        if verbose:
            print(
                f"[agent] iteration {iteration} — calling model (est={token_est} tokens)..."
            )

        msg, usage = chat_completion(messages, model)

        real = usage.get("prompt_tokens", "?")
        diff = (token_est - real) if isinstance(real, int) else "?"
        log.info(
            "token usage: est=%s real=%s diff=%s | completion=%s total=%s",
            token_est,
            real,
            diff,
            usage.get("completion_tokens", "?"),
            usage.get("total_tokens", "?"),
        )
        if verbose:
            print(f"[agent] token usage: est={token_est} real={real} diff={diff}")

        tool_calls = msg.get("tool_calls") or []
        messages.append(msg)

        if not tool_calls:
            final = msg.get("content") or ""

            # auto_confirm: if no DONE_MARKER, nudge the model to confirm / continue
            if auto_confirm and DONE_MARKER not in final and confirms < max_confirms:
                confirms += 1
                log.warning(
                    "no tool_calls and no done marker — confirm attempt %d/%d",
                    confirms,
                    max_confirms,
                )
                if verbose:
                    print(
                        f"[agent] no completion marker — confirm attempt {confirms}/{max_confirms}"
                    )
                messages.append({"role": "user", "content": CONFIRM_PROMPT})
                continue  # re-enter the loop with the confirmation message

            log.info(
                "=== agent done | iterations=%d | confirms=%d | answer=%s",
                iteration,
                confirms,
                final,
            )
            if verbose:
                print(
                    f"\n[agent] finished after {iteration} iteration(s), {confirms} confirm(s)"
                )
                print(f"\n{'=' * 60}")
                print("FINAL ANSWER:")
                print("=" * 60)
                print(final)
            return final

        # Execute each tool call and append a tool-role message with matching id
        for tc in tool_calls:
            tc_id = tc.get("id", "")
            fn_name = tc["function"]["name"]
            raw_args = tc["function"].get("arguments") or {}
            if isinstance(raw_args, str):
                try:
                    fn_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    fn_args = {}
            else:
                fn_args = raw_args

            log.info(
                "tool call: %s | args=%s",
                fn_name,
                json.dumps(fn_args, ensure_ascii=False),
            )
            if verbose:
                args_preview = json.dumps(fn_args, ensure_ascii=False)
                if len(args_preview) > 120:
                    args_preview = args_preview[:120] + "..."
                print(f"  [tool call] {fn_name}({args_preview})")

            result_str = dispatch_tool(fn_name, fn_args, messages)

            log.info("tool result: %s | %s", fn_name, result_str)
            if verbose:
                preview = result_str[:200] + ("..." if len(result_str) > 200 else "")
                print(f"  [tool result] {preview}")

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_str,
                }
            )


# ─── CLI ───────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Browser automation agent powered by Ollama",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py "Open Hacker News and return the top 5 story titles"
  python agent.py --model qwen/qwen3-235b-a22b "Go to example.com and return the page title"
  python agent.py -q "What is the current URL of any open tab?"
        """,
    )
    parser.add_argument("prompt", help="Initial prompt for the agent")
    parser.add_argument(
        "--model",
        "-m",
        default=OPENROUTER_MODEL,
        help=f"OpenRouter model (default: {OPENROUTER_MODEL}, overrides OPENROUTER_MODEL env)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress tool call logs, only print final answer",
    )
    parser.add_argument(
        "--auto-confirm",
        "-a",
        action="store_true",
        help="Auto-retry with confirmation prompt when model stops without completing the task",
    )
    parser.add_argument(
        "--max-confirms",
        type=int,
        default=3,
        help="Max confirmation re-prompts when --auto-confirm is set (default: 3)",
    )

    args = parser.parse_args()

    final = run_agent(
        prompt=args.prompt,
        model=args.model,
        verbose=not args.quiet,
        auto_confirm=args.auto_confirm,
        max_confirms=args.max_confirms,
    )

    if args.quiet:
        print(final)

    return 0


if __name__ == "__main__":
    sys.exit(main())

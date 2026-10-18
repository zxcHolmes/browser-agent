package handler

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/sirupsen/logrus"
)

// HealthHandler handles health check requests
type HealthHandler struct {
	log *logrus.Logger
}

// NewHealthHandler creates a new health handler
func NewHealthHandler(log *logrus.Logger) *HealthHandler {
	return &HealthHandler{log: log}
}

const docsHTMLTpl = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Browser Agent Proxy — API Docs</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #e6edf3; line-height: 1.6; }
  .container { max-width: 860px; margin: 0 auto; padding: 40px 24px 80px; }
  h1 { font-size: 2rem; font-weight: 700; margin-bottom: 8px; color: #f0f6fc; }
  h2 { font-size: 1.25rem; font-weight: 600; margin: 36px 0 12px; color: #f0f6fc; border-bottom: 1px solid #30363d; padding-bottom: 6px; }
  h3 { font-size: 1rem; font-weight: 600; margin: 24px 0 8px; color: #cdd9e5; }
  p { margin-bottom: 12px; color: #8b949e; }
  a { color: #58a6ff; text-decoration: none; }
  pre { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 16px; overflow-x: auto; margin: 12px 0; }
  code { font-family: "SF Mono", Consolas, monospace; font-size: 0.875rem; }
  p code, li code { background: #161b22; border: 1px solid #30363d; border-radius: 4px; padding: 1px 6px; font-size: 0.85em; color: #e6edf3; }
  .badge { display: inline-block; background: #1f6feb; color: #fff; font-size: 0.75rem; font-weight: 600; padding: 2px 8px; border-radius: 12px; margin-right: 6px; font-family: monospace; }
  .badge.delete { background: #da3633; }
  .badge.get { background: #238636; }
  table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.9rem; }
  th { text-align: left; padding: 8px 12px; background: #161b22; color: #8b949e; border-bottom: 1px solid #30363d; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; color: #cdd9e5; }
  td code { font-size: 0.82rem; }
  .flow { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
  .step { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 6px 14px; font-size: 0.85rem; color: #cdd9e5; }
  .arrow { color: #30363d; font-size: 1.2rem; }
  .status { display: inline-block; background: #238636; color: #fff; font-size: 0.7rem; font-weight: 700; padding: 2px 6px; border-radius: 4px; vertical-align: middle; margin-left: 8px; }
  ul { margin: 8px 0 12px 20px; color: #8b949e; }
  li { margin-bottom: 4px; }
</style>
</head>
<body>
<div class="container">
  <h1>Browser Agent Proxy</h1>
  <p>A Go server + Chrome Extension that lets you control a normal Chrome browser via a simple REST API. No <code>--remote-debugging-port</code> or Playwright required.</p>
  <p>Base URL: <code>{{BASE_URL}}</code> &nbsp;<span class="status">OK</span></p>

  <h2>Architecture</h2>
  <div class="flow">
    <div class="step">Your Code (HTTP REST)</div><div class="arrow">↓</div>
    <div class="step">Go Server :12345</div><div class="arrow">↓</div>
    <div class="step">Chrome Extension</div><div class="arrow">↓</div>
    <div class="step">Chrome Browser</div>
  </div>
  <p>All responses follow: <code>{"success": true, "data": {...}}</code> or <code>{"success": false, "error": "...", "code": "ERROR_CODE"}</code></p>
  <p><strong>Important</strong>: Only tabs created via <code>POST /api/tabs</code> are managed. User-opened tabs are invisible to the API.</p>

  <h2>Core Workflow</h2>
  <div class="flow">
    <div class="step">1. Create tab</div><div class="arrow">→</div>
    <div class="step">2. Navigate</div><div class="arrow">→</div>
    <div class="step">3. Wait for load</div><div class="arrow">→</div>
    <div class="step">4. Interact</div><div class="arrow">→</div>
    <div class="step">5. Close tab</div>
  </div>

  <h2>Tab Management</h2>
  <h3><span class="badge">POST</span> /api/tabs — Create a tab</h3>
  <pre><code>{ "url": "https://example.com", "active": false, "meta": { "job": "scrape-001" } }</code></pre>
  <p>Returns the tab object with <code>tabId</code> — keep this, it's the key to everything else.</p>

  <h3>List / Get / Close</h3>
  <pre><code>GET    /api/tabs          # list all managed tabs
GET    /api/tabs/{tabId}  # get one tab
DELETE /api/tabs/{tabId}  # close a tab</code></pre>

  <h2>High-Level Actions (recommended)</h2>
  <h3><span class="badge">POST</span> /api/tabs/{tabId}/navigate</h3>
  <pre><code>{ "url": "https://example.com", "waitForLoad": true, "timeoutSeconds": 30 }</code></pre>
  <p>Blocks until <code>Page.loadEventFired</code> fires — no manual event polling needed.</p>

  <h3><span class="badge get">GET</span> /api/tabs/{tabId}/screenshot</h3>
  <pre><code>GET /api/tabs/{tabId}/screenshot?format=jpeg&amp;quality=80</code></pre>
  <p>Returns raw image bytes (<code>Content-Type: image/png</code> or <code>image/jpeg</code>).</p>

  <h3><span class="badge">POST</span> /api/tabs/{tabId}/eval</h3>
  <pre><code>{ "expression": "document.title", "awaitPromise": false, "timeoutSeconds": 30 }</code></pre>
  <p>Returns: <code>{ "type": "string", "value": "Example Domain" }</code></p>

  <h2>Raw CDP Commands</h2>
  <h3><span class="badge">POST</span> /api/cdp</h3>
  <pre><code>{
  "tabId": 123,
  "method": "Network.getCookies",
  "params": {},
  "timeout": 30
}</code></pre>
  <p>Debugger is auto-attached on first command. Common methods: <code>Page.navigate</code>, <code>Page.captureScreenshot</code>, <code>Runtime.evaluate</code>, <code>Input.dispatchKeyEvent</code>, <code>Network.getCookies</code>, <code>DOM.querySelector</code>.</p>

  <h2>Event Polling</h2>
  <h3><span class="badge get">GET</span> /api/events</h3>
  <pre><code>GET /api/events?tabId=123&amp;method=Page.loadEventFired&amp;since=1700000000000</code></pre>
  <table>
    <tr><th>Param</th><th>Description</th></tr>
    <tr><td><code>tabId</code></td><td>Filter by tab (omit for all tabs)</td></tr>
    <tr><td><code>method</code></td><td>Exact match or domain prefix (<code>Page.</code>, <code>Network.</code>). Repeatable (OR logic)</td></tr>
    <tr><td><code>since</code></td><td>Unix ms — return only events newer than this</td></tr>
  </table>
  <p>Events are stored server-side for 3 minutes. System events: <code>tab.closed</code> (reason: timeout/api/user), <code>debugger.detached</code>.</p>

  <h2>Tab Idle Timeout</h2>
  <p>Eligible tabs with no CDP activity for 30 minutes are automatically closed by default. In-flight operations are protected. Subsequent calls return <code>404 TAB_CLOSED</code> — recreate the tab. Configurable via <code>tabTimeoutMinutes</code> in extension options.</p>

  <h2>Error Codes</h2>
  <table>
    <tr><th>Code</th><th>HTTP</th><th>Meaning</th></tr>
    <tr><td><code>EXTENSION_NOT_CONNECTED</code></td><td>503</td><td>Extension not loaded or crashed</td></tr>
    <tr><td><code>TAB_NOT_FOUND</code></td><td>404</td><td>Tab doesn't exist or wasn't created via API</td></tr>
    <tr><td><code>TAB_CLOSED</code></td><td>404</td><td>Tab timed out or was closed — recreate it</td></tr>
    <tr><td><code>CDP_COMMAND_FAILED</code></td><td>500</td><td>Chrome rejected the CDP command</td></tr>
    <tr><td><code>JS_EXCEPTION</code></td><td>422</td><td>JavaScript expression threw</td></tr>
    <tr><td><code>INVALID_REQUEST</code></td><td>400</td><td>Bad request body</td></tr>
    <tr><td><code>TAB_ID_REQUIRED</code></td><td>400</td><td>Missing tabId</td></tr>
    <tr><td><code>METHOD_REQUIRED</code></td><td>400</td><td>Missing CDP method</td></tr>
    <tr><td><code>URL_REQUIRED</code></td><td>400</td><td>Missing url (navigate)</td></tr>
    <tr><td><code>EXPRESSION_REQUIRED</code></td><td>400</td><td>Missing expression (eval)</td></tr>
  </table>

  <h2>Python Quick-Start</h2>
  <pre><code>import requests, time, json

BASE = "{{BASE_URL}}"

def create_tab(url, meta=None):
    r = requests.post(f"{BASE}/api/tabs", json={"url": url, "active": True, "meta": meta or {}})
    r.raise_for_status()
    return r.json()["data"]["tab"]

def close_tab(tab_id):
    requests.delete(f"{BASE}/api/tabs/{tab_id}")

def navigate(tab_id, url, timeout=30):
    r = requests.post(f"{BASE}/api/tabs/{tab_id}/navigate",
                      json={"url": url, "waitForLoad": True, "timeoutSeconds": timeout})
    r.raise_for_status()
    return r.json()["data"]

def evaluate(tab_id, expression, timeout=30):
    r = requests.post(f"{BASE}/api/tabs/{tab_id}/eval",
                      json={"expression": expression, "awaitPromise": True, "timeoutSeconds": timeout})
    r.raise_for_status()
    return r.json()["data"].get("value")

# Example: scrape Hacker News
tab = create_tab("https://news.ycombinator.com")
tab_id = tab["tabId"]
try:
    navigate(tab_id, "https://news.ycombinator.com")
    stories = json.loads(evaluate(tab_id, """
        JSON.stringify([...document.querySelectorAll('tr.athing')].map(r => ({
            title: r.querySelector('.titleline > a')?.innerText,
            url:   r.querySelector('.titleline > a')?.href,
        })))
    """))
    print(json.dumps(stories[:5], indent=2))
finally:
    close_tab(tab_id)</code></pre>
</div>
</body>
</html>`

// ServeHTTP handles health check and docs requests
func (h *HealthHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodHead {
		w.WriteHeader(http.StatusOK)
		return
	}

	scheme := "http"
	if r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https") {
		scheme = "https"
	}
	host := r.Host
	if host == "" {
		host = "localhost"
	}
	baseURL := fmt.Sprintf("%s://%s", scheme, host)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write([]byte(strings.ReplaceAll(docsHTMLTpl, "{{BASE_URL}}", baseURL)))
}

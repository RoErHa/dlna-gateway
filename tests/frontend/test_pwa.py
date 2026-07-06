"""PWA install + Service Worker registration."""
import json


# ── HTML / manifest contract ──────────────────────────────────────

def test_manifest_link_present(app):
    """Browser PWA installability requires a <link rel='manifest'> in the HTML."""
    href = app.locator("link[rel='manifest']").get_attribute("href")
    assert href and "manifest.json" in href


def test_manifest_has_required_pwa_fields(app):
    """Manifest must include the fields Chromium uses to gate the install
    prompt: name, short_name, start_url, display, icons (with at least one
    192px and one 512px entry)."""
    txt = app.evaluate(
        "fetch('/manifest.json').then(r => r.text())"
    )
    data = json.loads(txt)
    for key in ("name", "short_name", "start_url", "display", "icons"):
        assert key in data, f"manifest missing {key}"
    assert data["display"] in ("standalone", "fullscreen", "minimal-ui")
    sizes = {i.get("sizes") for i in data["icons"]}
    assert "192x192" in sizes, f"missing 192px icon — sizes were {sizes}"
    assert "512x512" in sizes, f"missing 512px icon — sizes were {sizes}"


def test_apple_touch_icon_present(app):
    """iOS Add-to-Home-Screen reads <link rel='apple-touch-icon'>, not the manifest."""
    icon = app.locator("link[rel='apple-touch-icon']")
    assert icon.count() >= 1
    href = icon.first.get_attribute("href")
    assert href, "apple-touch-icon has no href"


def test_apple_mobile_web_app_meta(app):
    """For iOS PWA standalone mode the 'apple-mobile-web-app-capable' meta
    must be 'yes'; otherwise Add-to-Home-Screen launches in a tab."""
    cap = app.locator("meta[name='apple-mobile-web-app-capable']")
    assert cap.count() == 1
    assert cap.get_attribute("content") == "yes"


def test_theme_color_meta_present(app):
    """The browser's URL bar / status bar tint comes from <meta name='theme-color'>."""
    tc = app.locator("meta[name='theme-color']")
    assert tc.count() >= 1
    assert tc.first.get_attribute("content"), "theme-color empty"


def test_viewport_meta_present(app):
    vp = app.locator("meta[name='viewport']")
    assert vp.count() == 1
    content = vp.get_attribute("content") or ""
    assert "width=device-width" in content


# ── Service Worker registration ───────────────────────────────────

def test_sw_js_served(app):
    """sw.js must be reachable at the path app.js registers (`/sw.js`)."""
    status = app.evaluate(
        "fetch('/sw.js').then(r => r.status)"
    )
    assert status == 200


def test_sw_registers(app):
    """After page load, navigator.serviceWorker should have a registration
    pointing at /sw.js. The PWA install banner depends on this."""
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    state = app.evaluate(
        "navigator.serviceWorker.getRegistration().then(r => r ? r.active.state : null)"
    )
    assert state == "activated", f"SW state was {state!r}"


def test_sw_caches_app_shell(app):
    """After SW activates, the app shell entries (/, /static/app.js, etc.)
    should be in the live APP_CACHE. Install-time pre-cache behaviour from
    sw.js — if it breaks, the offline mode silently fails. Reads the cache
    name from sw.js so a version bump there doesn't break this test."""
    # Wait for activation
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    # Give the cache.addAll() a beat to settle
    app.wait_for_timeout(500)
    # Look up the live cache name straight from window, so this test
    # tracks sw.js version bumps automatically.
    cached = app.evaluate("""
      caches.keys()
        .then(names => names.find(n => n.startsWith('dlna-gw-app-')))
        .then(name => caches.open(name))
        .then(c => c.keys())
        .then(keys => keys.map(k => new URL(k.url).pathname))
    """)
    # Spot-check the critical shell entries
    for must_have in ("/static/app.js", "/manifest.json"):
        assert must_have in cached, (
            f"shell entry {must_have!r} not in cache; cached={cached}")


def test_sw_navigation_is_network_first(app):
    """Regression (2026-06-27): a broken/empty cached '/' document pinned the
    app blank on every load because the HTML shell was served
    stale-while-revalidate (cached-first). The navigation must be
    NETWORK-FIRST: when online, a poisoned cache entry for '/' must be ignored
    in favour of the fresh network document, so a bad cache can't trap the user
    with "full UI, no content"."""
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    # Poison the app-shell cache entry for '/' with a broken document.
    app.evaluate("""
      caches.keys()
        .then(names => names.find(n => n.startsWith('dlna-gw-app-')))
        .then(name => caches.open(name))
        .then(c => c.put('/', new Response('<html>POISONED-STALE</html>',
              {headers: {'Content-Type': 'text/html'}})))
    """)
    # Fetch '/' through the SW. Network-first must return the real document.
    body = app.evaluate("fetch('/').then(r => r.text())")
    assert "POISONED-STALE" not in body, (
        "SW served the poisoned cached '/' — navigation is not network-first")
    assert "app.js" in body and "DLNA Gateway" in body, (
        f"network document not returned; got: {body[:160]!r}")


def test_poisoned_shell_cache_still_renders(app):
    """OUTCOME test (not code-shape). The real 2026-06-27 outage was a broken
    APP_CACHE pinning the app blank: 'full UI, no content', unrecoverable by
    refresh. This poisons BOTH the '/' document AND /static/app.js in the live
    cache, reloads, and asserts the app still BOOTS WITH CONTENT — i.e. an
    online load must never be held hostage by a bad cached shell asset. The
    earlier network-first fix only covered '/', so a poisoned app.js still
    blanked the app; this guards the asset path too."""
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    # Poison the two critical shell entries with broken bodies.
    app.evaluate("""
      caches.keys()
        .then(names => names.find(n => n.startsWith('dlna-gw-app-')))
        .then(name => caches.open(name))
        .then(c => Promise.all([
          c.put('/', new Response(
            '<!doctype html><html><body>POISONED-DOC</body></html>',
            {headers: {'Content-Type': 'text/html'}})),
          c.put('/static/app.js', new Response(
            'throw new Error("POISONED-JS");',
            {headers: {'Content-Type': 'application/javascript'}})),
        ]))
    """)
    # Reload: the SW must serve the FRESH shell + JS, not the poison.
    app.reload()
    # The app is booted iff refreshServers() populated the source picker —
    # which only happens if the real app.js executed.
    app.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=6000,
    )
    body = app.inner_text("body")
    assert "POISONED" not in body, "SW served a poisoned shell asset"


def test_sw_update_rolls_out_from_broken_worker(page, stub):
    """OUTCOME test for the SW UPGRADE path (the part that was only asserted
    before). Start a client wedged on a faithful copy of the PRE-FIX broken
    worker (stale-while-revalidate shell + skipWaiting gated behind addAll),
    poison its cache (the exact 2026-06-27 outage), then DEPLOY the real fixed
    worker and let the client update itself. With NO storage clear the new
    worker must activate, evict the old poisoned cache, and render real content.

    Proves the code upgrade path is clean. NB: runs on Chromium — it does NOT
    cover iOS/WebKit, which empirically does not perform the update check on its
    own (hence a pre-fix-wedged iPhone still needs a one-time 'clear site
    data'). That gap is a platform limitation, not a code defect."""
    import pathlib
    new_sw = pathlib.Path("static/sw.js").read_text()
    import re
    assert re.search(r"dlna-gw-app-v\d+", new_sw), \
        "expected a dlna-gw-app-v<N> cache version in sw.js"
    old_sw = """
const APP_CACHE='dlna-gw-app-vOLD';
const SHELL=['/','/static/app.css','/static/app.js','/static/vendor/hls.min.js','/manifest.json','/icon-192.png','/icon-512.png'];
self.addEventListener('install',e=>e.waitUntil(caches.open(APP_CACHE).then(c=>c.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(u.pathname.startsWith('/api/')||u.pathname.startsWith('/stream')||e.request.method!=='GET')return;
  e.respondWith(caches.open(APP_CACHE).then(c=>c.match(e.request).then(cached=>{
    const net=fetch(e.request).then(r=>{if(r.ok)c.put(e.request,r.clone());return r;}).catch(()=>cached);
    return cached||net;
  })));
});
"""
    serve = {"sw": old_sw}
    page.route("**/sw.js", lambda r: r.fulfill(
        status=200, body=serve["sw"], content_type="application/javascript",
        headers={"Cache-Control": "no-store"}))

    # 1) Wedge the client on the broken worker.
    page.goto(stub.base_url + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        ".then(r => r && r.active && r.active.state === 'activated')", timeout=8000)

    # 2) Poison the old shell cache (the outage condition).
    page.evaluate("""caches.open('dlna-gw-app-vOLD').then(c => Promise.all([
        c.put('/', new Response('<html><body>POISON-DOC</body></html>',
              {headers:{'Content-Type':'text/html'}})),
        c.put('/static/app.js', new Response('throw new Error("POISON-JS")',
              {headers:{'Content-Type':'application/javascript'}}))]))""")

    # 3) Deploy the fixed worker; the client updates itself — NO storage clear.
    serve["sw"] = new_sw
    page.evaluate("navigator.serviceWorker.getRegistration().then(r => r.update())")
    page.wait_for_function(
        "caches.keys().then(ks =>"
        " ks.includes('dlna-gw-app-v14') && !ks.includes('dlna-gw-app-vOLD'))",
        timeout=8000)

    # 4) Reload (still no clear) — the app must boot with real content.
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=8000)
    body = page.inner_text("body")
    assert "POISON" not in body, "new worker served the poisoned old-cache shell"


def test_sw_does_not_intercept_api_calls(app, gateway):
    """sw.js explicitly excludes /api/* — verify a fresh /api/servers call
    actually hits the network (not a stale cache). Matters because a
    misrouted SW would freeze server status / playlist updates after the
    network goes away."""
    # Wait for SW to activate
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    gateway.clear_requests()
    app.evaluate("fetch('/api/servers').then(r => r.json())")
    req = gateway.wait_for_request("/api/servers", timeout=2.0)
    assert req is not None, "SW swallowed an /api/* call — must always pass through"


def test_sw_caches_art_proxy(app, gateway):
    """sw.js should cache /art images (cache-first). After two fetches of the
    same art URL, only one should reach the gateway — the second comes from
    ART_CACHE."""
    app.wait_for_function(
        "navigator.serviceWorker && navigator.serviceWorker.getRegistration()"
        " .then(r => r && r.active && r.active.state === 'activated')",
        timeout=8000,
    )
    art_url = "/art?url=" + "http%3A%2F%2Fstub%2Fcover.jpg"
    gateway.clear_requests()
    # First fetch — populates the cache
    app.evaluate(f"fetch('{art_url}').then(r => r.blob())")
    app.wait_for_timeout(500)
    first = len(gateway.captured(path_contains="/art"))
    # Second fetch — should be served from cache, no extra hit
    app.evaluate(f"fetch('{art_url}').then(r => r.blob())")
    app.wait_for_timeout(500)
    second = len(gateway.captured(path_contains="/art"))
    assert second == first, (
        f"second /art fetch hit network ({second - first} new requests); "
        "ART_CACHE not working")

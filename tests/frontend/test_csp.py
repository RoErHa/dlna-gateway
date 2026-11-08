"""
tests/frontend/test_csp.py — the Content-Security-Policy, in a real engine.

The stub serves the gateway's policy enforced (stub_gateway.py), and
conftest.py fails any test that trips it, so the whole suite passing is the
proof that the policy does not break the PWA. These tests prove the other
half: that the policy is really there and really stops an injected handler,
that the violation check really sees one, and that the replacements for the
inline handlers (data-act / data-onerr) work.
"""
import pytest


def test_the_page_carries_the_policy(page, stub):
    resp = page.goto(stub.base_url + "/")
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.csp_violations_expected
def test_an_injected_inline_handler_does_not_run(app):
    """If an escaping bug ever lets markup through, it stays markup."""
    seen = []
    app.on("console", lambda m: seen.append(m.text))
    app.evaluate("""() => {
        const d = document.createElement('div');
        d.innerHTML = '<img src="/nope.png" onerror="window.__pwned=1">';
        document.body.appendChild(d);
    }""")
    app.wait_for_timeout(500)
    assert app.evaluate("() => window.__pwned") is None
    assert any("CSP violation" in t or "Refused to" in t
               or "Content Security Policy" in t for t in seen), seen


def test_broken_cover_falls_back_without_an_inline_handler(app):
    app.evaluate("""() => {
        const d = document.createElement('div');
        d.id = 'fallback-probe';
        d.innerHTML = '<img src="/nope-a.png" data-onerr-text="💿">';
        const e = document.createElement('div');
        e.innerHTML = '<img id="hide-probe" src="/nope-b.png" data-onerr="hide">';
        document.body.append(d, e);
    }""")
    app.wait_for_function(
        "document.getElementById('fallback-probe').textContent === '💿'", timeout=3000)
    app.wait_for_function(
        "document.getElementById('hide-probe').style.display === 'none'", timeout=3000)


def test_data_act_runs_allowlisted_actions_only(app):
    app.evaluate("""() => {
        for (const [id, act, arg] of [['ok', 'showTab', 'search'],
                                      ['proto', 'constructor', ''],
                                      ['unknown', 'alert', 'x']]) {
            const b = document.createElement('button');
            b.id = 'act-' + id; b.dataset.act = act; b.dataset.arg = arg;
            document.body.appendChild(b);
        }
    }""")
    app.click("#act-proto")
    app.click("#act-unknown")
    app.click("#act-ok")
    app.wait_for_function(
        "document.getElementById('tab-search').classList.contains('active')", timeout=3000)


def test_the_fonts_load_from_the_gateway(app):
    """DM Mono and Syne are served from static/fonts since 2026-11-08 (they
    were Google Fonts). With font-src 'self' enforced, a face that failed to
    load would be a silent fallback to a system font — so ask the browser."""
    app.wait_for_function("document.fonts.status === 'loaded'")
    loaded = app.evaluate("""async () => {
        await document.fonts.load("400 13px 'DM Mono'");
        await document.fonts.load("800 15px 'Syne'");
        return [...document.fonts].filter(f => f.status === 'loaded')
                                  .map(f => f.family.replace(/["']/g, ''));
    }""")
    assert "DM Mono" in loaded, loaded
    assert "Syne" in loaded, loaded

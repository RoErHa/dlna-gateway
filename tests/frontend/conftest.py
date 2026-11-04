"""
Pytest fixtures for frontend Playwright tests.

Each test gets:
  - a fresh `gateway` (StubGateway) on an ephemeral port
  - a fresh `app` (Playwright page) navigated to the gateway and primed

Run with:
  .venv/bin/pytest tests/frontend
  .venv/bin/pytest tests/frontend --headed     # see the browser
  .venv/bin/pytest tests/frontend -k transport # subset
"""
from __future__ import annotations

import pytest

from tests.frontend.stub_gateway import StubServer


@pytest.fixture
def stub():
    """Underlying StubServer — exposes both .gateway (state) and .base_url."""
    s = StubServer()
    s.start()
    yield s
    s.stop()


@pytest.fixture
def gateway(stub):
    """Per-test stub gateway state. Mutate before navigating to influence the UI."""
    return stub.gateway


# Every CSP violation in a test is a failure (2026-11-04): the stub serves the
# gateway's policy ENFORCED, so a violation means a feature the policy breaks.
# Both engines log one as "Refused to …"; the event catches the rest.
_CSP_LISTENER = ("document.addEventListener('securitypolicyviolation', e => "
                 "console.error('CSP violation: ' + e.effectiveDirective + ' ' "
                 "+ e.blockedURI))")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "csp_violations_expected: the test injects markup the "
        "Content-Security-Policy is meant to block (XSS tests)")


def _csp_check(page, request):
    violations: list[str] = []
    page.add_init_script(_CSP_LISTENER)
    page.on("console", lambda m: violations.append(m.text)
            if m.type == "error" and ("CSP violation" in m.text
                                      or "Content Security Policy" in m.text
                                      or m.text.startswith("Refused to"))
            else None)
    return violations


def _fail_on_csp(violations, request):
    if violations and not request.node.get_closest_marker("csp_violations_expected"):
        pytest.fail("Content-Security-Policy violation(s):\n" + "\n".join(violations),
                    pytrace=False)


@pytest.fixture(autouse=True)
def csp_guard(request):
    """Any test that uses `page` (directly or via app/mobile_app) is watched."""
    if "page" not in request.fixturenames:
        yield
        return
    page = request.getfixturevalue("page")
    violations = _csp_check(page, request)
    yield
    _fail_on_csp(violations, request)


def _boot(page, stub):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(stub.base_url + "/")
    # Readiness: the redesigned header dropped the standalone disc-label;
    # server status now lives in the #source-sel options. The app is booted
    # once refreshServers() has replaced the "Scanning…" placeholder with a
    # real server option (equivalent to the old disc-label != 'Scanning…').
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000,
    )
    return errors


@pytest.fixture
def app(page, stub, gateway, request):
    """Desktop-viewport Playwright page navigated to the stub gateway.
    Uncaught JS errors are surfaced as a warning attached to the test node
    so they're visible without masking the actual assertion failure."""
    page.set_viewport_size({"width": 1280, "height": 800})
    errors = _boot(page, stub)
    yield page
    if errors:
        request.node.add_report_section(
            "teardown", "page-errors", "\n".join(errors))


@pytest.fixture
def mobile_app(page, stub, gateway, request):
    """Mobile-viewport Playwright page (iPhone-ish)."""
    page.set_viewport_size({"width": 375, "height": 667})
    errors = _boot(page, stub)
    yield page
    if errors:
        request.node.add_report_section(
            "teardown", "page-errors", "\n".join(errors))

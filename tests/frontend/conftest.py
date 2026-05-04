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


def _boot(page, stub):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('disc-label') && "
        "document.getElementById('disc-label').textContent !== 'Scanning…'",
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

"""The screen while the engine waits for a Stellar venue to be entered.

The engine publishes a short snapshot then — alive, not simulated, and why
it is waiting. Every page must render it without a script error, and the
landing page is the Connection page, which says what to do."""
import json
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from fixtrader.config import TraderConfig  # noqa: E402
from fixtrader.runner import WAITING_FOR_VENUE  # noqa: E402
from fixtrader.webapp import create_app  # noqa: E402
from tests.test_ui_browser import _launch  # noqa: E402


@pytest.fixture
def server(tmp_path):
    TraderConfig(path=str(tmp_path / 'config.json')).save()
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True, 'simulated': False, 'waiting_for_venue': True,
                   'text': WAITING_FOR_VENUE,
                   'session': {'state': 'DOWN', 'text': WAITING_FOR_VENUE}},
        'contracts': []}))
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'commands.jsonl'), str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def test_every_page_renders_while_the_engine_waits_for_a_venue(server):
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(server, wait_until='domcontentloaded')
        page.wait_for_function(
            "document.querySelector('#overall-state') && "
            "document.querySelector('#overall-state').textContent.includes('Waiting')")
        assert 'Exchanges page' in page.inner_text('#overall-detail')
        for path in ('desk', 'exchanges', 'instruments', 'account', 'settings'):
            page.goto(server + path, wait_until='domcontentloaded')
            page.wait_for_timeout(1200)               # a poll or two
        browser.close()
    assert errors == []

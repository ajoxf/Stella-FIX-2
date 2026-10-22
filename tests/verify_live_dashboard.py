"""Explicit operator smoke check: exercises the running TT UAT dashboard.

Not collected by pytest. Run manually only when a UAT disconnect/reconnect is
intended. Leaves the sessions connected and writes screenshots to artifacts/.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:8000/', wait_until='domcontentloaded')
        expect(page.locator('h1')).to_have_text('FIX connection dashboard')
        expect(page.locator('#environment')).to_have_text('UAT')
        expect(page.locator('#overall-state')).to_have_text('Connected', timeout=45000)
        expect(page.locator('.session-card')).to_have_count(2)
        page.locator('#disconnect').click()
        expect(page.locator('#overall-state')).to_have_text('Disconnected', timeout=45000)
        expect(page.locator('#connect')).to_be_enabled()
        page.locator('#connect').click()
        expect(page.locator('#overall-state')).to_have_text('Connected', timeout=45000)
        expect(page.locator('#reconnect')).to_be_enabled()
        page.locator('#reconnect').click()
        expect(page.locator('#overall-state')).to_have_text('Connecting', timeout=45000)
        expect(page.locator('#overall-state')).to_have_text('Connected', timeout=45000)
        expect(page.locator('.session-status').nth(0)).to_have_text('Connected')
        expect(page.locator('.session-status').nth(1)).to_have_text('Connected')
        page.screenshot(path=str(artifacts / 'fix-dashboard-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(artifacts / 'fix-dashboard-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        # A dead engine must override previously green session badges.
        page.route('**/api/snapshot', lambda route: route.fulfill(status=200,
            json={'engine': {'alive': False, 'text': 'Engine stopped'}, 'contracts': []}))
        expect(page.locator('#overall-state')).to_have_text('Engine unavailable', timeout=5000)
        expect(page.locator('#connect')).to_be_disabled()
        page.unroute('**/api/snapshot')
        expect(page.locator('#overall-state')).to_have_text('Connected', timeout=5000)
        browser.close()
    assert not errors, errors
    print('PASS: live Disconnect, Connect, Reconnect; both TT sessions connected; mobile layout; stale-engine status; no JavaScript errors.')


if __name__ == '__main__':
    main()

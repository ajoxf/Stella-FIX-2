"""Read-only TT UAT smoke check: search, subscribe, review. Never sends orders."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright, expect


def main():
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width':1600, 'height':1150})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto('http://127.0.0.1:8000/instruments', wait_until='domcontentloaded')
        expect(page.locator('#session')).to_contain_text('LOGGED_ON', timeout=40000)
        page.locator('#search-form button').click()
        expect(page.locator('#results button').first).to_be_visible(timeout=40000)
        state = page.request.get('http://127.0.0.1:8000/api/snapshot').json()
        instruments = state['engine']['manual_terminal']['instruments']
        from datetime import datetime
        month = datetime.now().strftime('%Y%m')
        eligible = sorted([i for i in instruments if i.get('maturity','') >= month], key=lambda i:i['maturity'])
        instrument = (eligible or instruments)[0]
        page.locator('#filter').fill(instrument['security_id'])
        page.get_by_role('button', name='Add + subscribe', exact=True).click()
        expect(page.locator('#watchlist')).to_contain_text(instrument['security_id'], timeout=15000)
        page.locator('#watchlist').get_by_role('button', name='Buy', exact=True).first.click()
        page.locator('#ticket-form [name=price]').fill('0')
        page.locator('#review-order').click()
        expect(page.locator('#review-dialog')).to_be_visible(timeout=15000)
        expect(page.locator('#review-content')).to_contain_text(instrument['security_id'])
        page.locator('#abort').click()
        page.locator('#order-type').select_option('STOP_LIMIT')
        expect(page.locator('#stop-label')).to_be_visible()
        page.locator('#tif').select_option('GTD')
        expect(page.locator('#expiry-label')).to_be_visible()
        page.locator('#order-type').select_option('LIMIT')
        page.locator('#tif').select_option('DAY')
        expect(page.locator('#stop-label')).to_be_hidden()
        expect(page.locator('#expiry-label')).to_be_hidden()
        page.wait_for_timeout(5000)
        page.screenshot(path=str(artifacts/'instruments-desktop.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(artifacts/'instruments-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        state=page.request.get('http://127.0.0.1:8000/api/snapshot').json()
        terminal=state['engine']['manual_terminal']
        assert not terminal['orders'], 'Read-only check must not create any order'
        print(json.dumps({'search_count':len(instruments), 'instrument':instrument['description'],
                          'security_id':instrument['security_id'], 'watchlist':terminal['watchlist'],
                          'errors':errors}, default=str))
        browser.close()
    assert not errors


if __name__ == '__main__':
    main()

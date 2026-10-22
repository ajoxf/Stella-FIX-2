"""Review an existing fill's close ticket; never confirm or transmit it."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as p:
    browser=p.chromium.launch(channel='chrome',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto('http://127.0.0.1:8000/instruments',wait_until='domcontentloaded')
    expect(page.locator('#session')).to_contain_text('LOGGED_ON',timeout=40000)
    before=page.request.get('http://127.0.0.1:8000/api/snapshot').json()['engine']['manual_terminal']['orders']
    page.get_by_role('button',name='Close position',exact=True).first.click()
    expect(page.locator('#review-dialog')).to_be_visible(timeout=15000)
    expect(page.locator('#review-content')).to_contain_text('Open/Close: CLOSE')
    expect(page.locator('#review-content')).to_contain_text('at MARKET')
    page.screenshot(path=str(Path(__file__).resolve().parents[1]/'artifacts'/'close-position.png'))
    page.locator('#abort').click()
    after=page.request.get('http://127.0.0.1:8000/api/snapshot').json()['engine']['manual_terminal']['orders']
    assert len(before)==len(after)
    assert not errors, errors
    browser.close()
    print('PASS: Close position button opens the reviewed market-close ticket; no order sent.')

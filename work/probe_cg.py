from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    page = browser.new_page()
    for attempt in range(4):
        page.goto('https://www.coinglass.com/hyperliquid', wait_until='domcontentloaded', timeout=45000)
        page.wait_for_timeout(5000)
        if page.locator('table').count() >= 2 and page.locator('table').last.locator('tbody tr').count() >= 40:
            break
        print('retry', attempt, page.url, page.title(), page.locator('body').inner_text()[:100])
    print('rows', page.locator('table').last.locator('tbody tr').count())
    page.get_by_role('combobox').nth(3).click()
    print('options', page.get_by_role('option').count())
    page.get_by_role('option', name='BTC', exact=True).click()
    page.wait_for_timeout(2000)
    print('BTC combo', page.get_by_role('combobox').nth(3).input_value())
    print('BTC rows', page.locator('table').last.locator('tbody tr').count())
    print('BTC first', page.locator('table').last.locator('tbody tr').first.inner_text().replace('\n',' | ')[:160])
    print('BTC page', page.locator('.ant-pagination').last.inner_text())
    page.locator('.ant-pagination').last.locator('[title="2"]').click()
    page.wait_for_timeout(1800)
    print('BTC page2 first', page.locator('table').last.locator('tbody tr').first.inner_text().replace('\n',' | ')[:160])
    browser.close()

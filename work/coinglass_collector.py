"""Read the public CoinGlass table through its normal browser controls."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = 'https://www.coinglass.com/hyperliquid'

def extract(page):
    return page.locator('table').last.locator('tbody tr.ant-table-row').evaluate_all('''rows => rows.map(r => {
      const c=Array.from(r.querySelectorAll('td')).map(c=>c.innerText.trim());
      return {rank:c[1],symbol:c[3],side:c[4],volume:c[5]?.split('\\n')[0],entry_price:c[7]?.split('\\n')[0],liquidation:c[8],price:c[11],entry_time:c[12]?.replace(/\\s+/g,' ')};
    })''')

def wait_rows(page, symbol=None, first_rank=None):
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        rows=extract(page) if page.locator('table').count() else []
        if rows and (symbol is None or all(r['symbol']==symbol for r in rows)) and (first_rank is None or rows[0]['rank']==str(first_rank)):
            if page.locator('.ant-spin-spinning').count()==0:
                return rows
        page.wait_for_timeout(300)
    raise RuntimeError('Таблица не загрузилась или показала другую страницу')

def collect(output, symbols_limit=None, progress=print, cancelled=lambda:False):
    result={'source':URL,'started_at':datetime.now(timezone.utc).isoformat(),'page_limit':3,'threshold':0.07,'excluded_substring':'XYZ','coins':[]}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        # CoinGlass returns nginx 404 to Playwright's default HeadlessChrome
        # user agent, although the same page opens with a regular Chrome one.
        chrome_major=browser.version.split('.',1)[0]
        context=browser.new_context(
            user_agent=f'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       f'(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36',
            locale='en-US',
        )
        page=context.new_page()
        page.set_default_timeout(15000)
        for attempt in range(3):
            response=page.goto(URL,wait_until='domcontentloaded',timeout=45000)
            try:
                if response is None or response.status != 200:
                    raise RuntimeError(f'CoinGlass вернул HTTP {response.status if response else "без ответа"}')
                wait_rows(page)
                break
            except RuntimeError:
                if attempt==2: raise
                page.wait_for_timeout(3000)
        combo=page.locator('input[role="combobox"]').last
        combo.click()
        page.get_by_role('option').first.wait_for()
        symbols=[s.strip() for s in page.get_by_role('option').all_text_contents() if s.strip()!='All' and 'XYZ' not in s.upper()]
        combo.press('Escape')
        if symbols_limit is not None: symbols=[s for s in symbols if s in symbols_limit]
        result['eligible_symbols']=symbols
        for index,symbol in enumerate(symbols):
            if cancelled():
                result['cancelled']=True
                break
            item={'symbol':symbol,'pages':[],'error':None}
            result['coins'].append(item)
            try:
                first=page.locator('.ant-pagination-item[title="1"]').last
                if first.count() and 'ant-pagination-item-active' not in (first.get_attribute('class') or ''):
                    first.click()
                    page.wait_for_timeout(800)
                    wait_rows(page,first_rank=1)
                combo.click()
                combo.fill(symbol)
                page.get_by_role('option',name=symbol,exact=True).click()
                page.wait_for_timeout(1000)
                first=page.locator('.ant-pagination-item[title="1"]').last
                if first.count() and 'ant-pagination-item-active' not in (first.get_attribute('class') or ''):
                    first.click()
                    page.wait_for_timeout(1000)
                wait_rows(page,symbol,1)
                header=page.locator('th').filter(has=page.locator('.ant-table-column-title',has_text='Position')).first
                for _ in range(3):
                    if header.get_attribute('aria-sort')=='descending': break
                    header.click()
                    page.wait_for_timeout(800)
                    wait_rows(page,symbol,1)
                if header.get_attribute('aria-sort')!='descending': raise RuntimeError('Сортировка по убыванию не подтверждена')
                for number in range(1,4):
                    if cancelled(): break
                    rows=wait_rows(page,symbol,1 if number==1 else expected_rank)
                    item['pages'].append({'number':number,'captured_at':datetime.now(timezone.utc).isoformat(),'rows':rows})
                    next_button=page.locator('.ant-pagination-next').last
                    if number==3 or not next_button.count() or next_button.get_attribute('aria-disabled')=='true': break
                    expected_rank=int(rows[-1]['rank'])+1
                    next_button.click()
                    page.wait_for_timeout(1000)
                progress(f"{index+1}/{len(symbols)} {symbol}: {len(item['pages'])} стр., {sum(len(x['rows']) for x in item['pages'])} строк")
            except Exception as exc:
                item['error']=type(exc).__name__+': '+str(exc)[:180]
                progress(f"{index+1}/{len(symbols)} {symbol}: ошибка")
            Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
            if len(result['coins'])>=3 and all(c['error'] for c in result['coins'][-3:]):
                result['stopped_after_errors']=True
                break
            page.wait_for_timeout(1000)
        browser.close()
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result

if __name__=='__main__':
    import sys
    collect(sys.argv[1],sys.argv[2:] or None)

"""Read public Hyperliquid positions and match entry prices to CoinGlass rows."""
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


INFO_URL = 'https://api.hyperliquid.xyz/info'
ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}\Z')


def dex_for(symbol):
    return symbol.split(':', 1)[0] if ':' in symbol else ''


def read_account(wallet, dex, opener=urlopen):
    payload = {'type': 'clearinghouseState', 'user': wallet}
    if dex:
        payload['dex'] = dex
    request = Request(INFO_URL, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    for attempt in range(3):
        try:
            with opener(request, timeout=15) as response:
                account = json.load(response)
            if not isinstance(account, dict) or not isinstance(account.get('assetPositions'), list):
                raise ValueError('Некорректный ответ Hyperliquid')
            return account['assetPositions']
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)


def matching_position(positions, symbol, side):
    for item in positions:
        position = item.get('position') or {}
        if position.get('coin') != symbol:
            continue
        try:
            size = Decimal(str(position.get('szi', '0')))
            price = Decimal(str(position.get('entryPx', '')))
        except InvalidOperation:
            continue
        if not price.is_finite() or price <= 0:
            continue
        if (side == 'Long' and size > 0) or (side == 'Short' and size < 0):
            return {'entry_price': str(price), 'size_coins': str(abs(size))}
    return None


def matching_entry(positions, symbol, side):
    matched = matching_position(positions, symbol, side)
    return matched['entry_price'] if matched else None


def enrich(data, progress=lambda message: None, cancelled=lambda: False,
           reader=read_account, interval=0.16):
    """Enrich each row once per wallet and perp dex; leave unmatched prices blank."""
    grouped = {}
    for coin in data['coins']:
        for page in coin['pages']:
            for row in page['rows']:
                row['hyperliquid_entry_price'] = None
                row['hyperliquid_position_size'] = None
                row['hyperliquid_entry_checked_at'] = None
                row['hyperliquid_entry_status'] = 'Нет адреса'
                wallet = str(row.get('wallet') or '')
                if ADDRESS.fullmatch(wallet):
                    row['hyperliquid_entry_status'] = 'Не проверено'
                    grouped.setdefault((wallet.lower(), dex_for(row.get('symbol') or '')), []).append(row)

    total = len(grouped)
    checked = 0
    for (wallet, dex), rows in grouped.items():
        if cancelled():
            data['cancelled'] = True
            break
        try:
            positions = reader(wallet, dex)
            checked_at = datetime.now(timezone.utc).isoformat()
            for row in rows:
                matched = matching_position(positions, row.get('symbol'), row.get('side'))
                row['hyperliquid_entry_price'] = matched['entry_price'] if matched else None
                row['hyperliquid_position_size'] = matched['size_coins'] if matched else None
                row['hyperliquid_entry_checked_at'] = checked_at
                row['hyperliquid_entry_status'] = 'Проверено' if matched else 'Позиция не совпала'
        except Exception:
            for row in rows:
                row['hyperliquid_entry_status'] = 'Ошибка запроса'
        checked += 1
        if checked % 25 == 0 or checked == total:
            progress(f'Проверяю цены входа в Hyperliquid: {checked}/{total} адресов')
        if interval and checked < total:
            time.sleep(interval)

    all_rows = [row for coin in data['coins'] for page in coin['pages'] for row in page['rows']]
    result = {'rows': len(all_rows),
              'matched': sum(row['hyperliquid_entry_status'] == 'Проверено' for row in all_rows),
              'unmatched': sum(row['hyperliquid_entry_status'] == 'Позиция не совпала' for row in all_rows),
              'missing_wallet': sum(row['hyperliquid_entry_status'] == 'Нет адреса' for row in all_rows),
              'not_checked': sum(row['hyperliquid_entry_status'] == 'Не проверено' for row in all_rows),
              'request_errors': sum(row['hyperliquid_entry_status'] == 'Ошибка запроса' for row in all_rows),
              'queried_accounts': checked}
    data['hyperliquid_entry_coverage'] = result
    return result

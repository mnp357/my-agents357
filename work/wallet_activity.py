"""Read-only, persistent fill and position history for tracked Hyperliquid wallets."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from hyperliquid_entry import ADDRESS, INFO_URL, read_account
from wallet_history import wallet_name


SCHEMA = '''
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen TEXT NOT NULL,
  cursor_ms INTEGER NOT NULL, last_checked_ms INTEGER
);
CREATE TABLE IF NOT EXISTS tracked_wallets (
  address TEXT PRIMARY KEY, first_tracked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dexes (
  address TEXT NOT NULL, dex TEXT NOT NULL,
  PRIMARY KEY (address, dex)
);
CREATE TABLE IF NOT EXISTS fills (
  event_id TEXT PRIMARY KEY, address TEXT NOT NULL, time_ms INTEGER NOT NULL,
  coin TEXT NOT NULL, event_type TEXT NOT NULL, direction TEXT,
  size_coins TEXT, start_position TEXT, end_position TEXT, price TEXT,
  closed_pnl TEXT, fee TEXT, fee_token TEXT, liquidation_method TEXT,
  tx_hash TEXT, trade_id TEXT, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_time ON fills(time_ms DESC);
CREATE TABLE IF NOT EXISTS position_snapshots (
  run_at TEXT NOT NULL, address TEXT NOT NULL, coin TEXT NOT NULL,
  signed_size TEXT NOT NULL, entry_price TEXT, liquidation_price TEXT,
  unrealized_pnl TEXT, checked_at TEXT NOT NULL,
  PRIMARY KEY (run_at, address, coin)
);
CREATE TABLE IF NOT EXISTS checks (
  run_at TEXT NOT NULL, address TEXT NOT NULL, checked_at TEXT NOT NULL,
  fills_status TEXT NOT NULL, positions_status TEXT NOT NULL,
  fills_found INTEGER NOT NULL, note TEXT,
  PRIMARY KEY (run_at, address)
);
'''


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, TypeError):
        return None


def millis(iso_time):
    return int(datetime.fromisoformat(iso_time.replace('Z', '+00:00')).timestamp() * 1000)


def connect(path):
    path = Path(path)
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    if path.exists():
        os.chmod(path, 0o600)
    return connection


def add_tracked(connection, history, now_ms):
    """Watch every valid address that has entered a top-seven snapshot."""
    for run in history['runs']:
        for item in run['observations']:
            if str(item.get('symbol') or '').upper() not in ('BTC', 'ETH'):
                continue
            address = str(item.get('wallet') or '').lower()
            if not ADDRESS.fullmatch(address):
                continue
            connection.execute(
                'INSERT OR IGNORE INTO wallets(address,name,first_seen,cursor_ms) VALUES(?,?,?,?)',
                (address, wallet_name(address), run['run_at'], now_ms))
            connection.execute(
                'INSERT OR IGNORE INTO tracked_wallets(address,first_tracked) VALUES(?,?)',
                (address, run['run_at']))
            coin = str(item.get('symbol') or '')
            dex = coin.split(':', 1)[0] if ':' in coin else ''
            connection.execute('INSERT OR IGNORE INTO dexes(address,dex) VALUES(?,?)',
                               (address, dex))
    connection.commit()


def post_info(payload, opener=urlopen):
    request = Request(INFO_URL, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    for attempt in range(4):
        try:
            with opener(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
        except (URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(5 * 2 ** attempt)


def fill_id(address, fill):
    trade_id = fill.get('tid')
    if trade_id is not None:
        return f'{address}:{trade_id}'
    return ':'.join((address, str(fill.get('hash') or ''), str(fill.get('oid') or ''),
                     str(fill.get('time') or ''), str(fill.get('coin') or ''),
                     str(fill.get('sz') or '')))


def fill_type(fill, address):
    before = decimal(fill.get('startPosition'))
    size = decimal(fill.get('sz'))
    side = fill.get('side')
    after = before + (size if side == 'B' else -size) if before is not None and size is not None and side in ('B', 'A') else None
    liquidation = fill.get('liquidation')
    liquidated = (isinstance(liquidation, dict) and
                  (not liquidation.get('liquidatedUser') or
                   str(liquidation['liquidatedUser']).lower() == address.lower()))
    if liquidated:
        kind = 'Ликвидация полная' if after == 0 else 'Ликвидация частичная'
    elif before is None or after is None:
        kind = 'Исполнение; размер позиции не определён'
    elif before == 0:
        kind = 'Открыл лонг' if after > 0 else 'Открыл шорт'
    elif after == 0:
        kind = 'Закрыл позицию'
    elif before * after < 0:
        kind = 'Развернул позицию'
    elif abs(after) > abs(before):
        kind = 'Увеличил позицию'
    elif abs(after) < abs(before):
        kind = 'Сократил позицию'
    else:
        kind = 'Исполнение без изменения размера'
    return kind, str(after) if after is not None else None, (
        liquidation.get('method') if liquidated else None)


def fetch_fills(address, start_ms, end_ms, request=post_info, pause=time.sleep):
    """Split full 2000-fill time windows; report gaps instead of skipping data."""
    windows = [(max(0, start_ms - 60000), end_ms)]
    seen = set()
    result = []
    calls = 0
    complete = True
    note = None
    while windows:
        first, last = windows.pop()
        if first > last:
            continue
        if calls >= 100:
            complete, note = False, 'Более 100 запросов для одного кошелька'
            break
        try:
            response = request({'type': 'userFillsByTime', 'user': address,
                                'startTime': first, 'endTime': last,
                                'aggregateByTime': False})
        except Exception as exc:
            complete, note = False, type(exc).__name__
            break
        if not isinstance(response, list):
            complete, note = False, 'Некорректный ответ Hyperliquid'
            break
        calls += 1
        if len(response) == 2000 and first < last:
            middle = (first + last) // 2
            windows.extend(((middle + 1, last), (first, middle)))
            pause(1.2 + len(response) * 0.004)
            continue
        if len(response) == 2000:
            complete, note = False, 'Более 2000 исполнений в одну миллисекунду'
        for row in response:
            if not isinstance(row, dict):
                continue
            event_time = int(row.get('time') or 0)
            key = fill_id(address, row)
            if start_ms < event_time <= end_ms and key not in seen:
                seen.add(key)
                result.append(row)
        if not complete:
            break
        pause(1.2 + len(response) * 0.004)
    if len(result) >= 10000:
        complete, note = False, 'Возможен предел 10000 последних исполнений Hyperliquid'
    return result, complete, note


def record_fill(connection, address, fill):
    coin = str(fill.get('coin') or '')
    direction = str(fill.get('dir') or '')
    if coin.upper() not in ('BTC', 'ETH') or fill.get('startPosition') is None:
        return False
    kind, after, liquidation_method = fill_type(fill, address)
    connection.execute('''INSERT OR IGNORE INTO fills
      (event_id,address,time_ms,coin,event_type,direction,size_coins,start_position,
       end_position,price,closed_pnl,fee,fee_token,liquidation_method,tx_hash,trade_id,source)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
      (fill_id(address, fill), address, int(fill['time']), coin, kind, direction,
       str(fill.get('sz') or ''), str(fill.get('startPosition') or ''), after,
       str(fill.get('px') or ''), str(fill.get('closedPnl') or ''),
       str(fill.get('fee') or ''), str(fill.get('feeToken') or ''),
       liquidation_method, str(fill.get('hash') or ''),
       str(fill.get('tid') or ''), 'Hyperliquid userFillsByTime'))
    if ':' in coin:
        connection.execute('INSERT OR IGNORE INTO dexes(address,dex) VALUES(?,?)',
                           (address, coin.split(':', 1)[0]))
    return True


def collect_activity(connection, history, run_at, progress=lambda _: None,
                     cancelled=lambda: False, request=post_info,
                     account_reader=read_account, pause=time.sleep):
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    add_tracked(connection, history, millis(run_at) - 1)
    wallets = connection.execute('''SELECT w.address,w.cursor_ms FROM wallets w
        JOIN tracked_wallets t ON t.address=w.address
        ORDER BY t.first_tracked,w.address''').fetchall()
    completed = 0
    for address, cursor_ms in wallets:
        if cancelled():
            break
        fills_status, positions_status, note = 'не проверено', 'не проверено', None
        fills = []
        try:
            fills, complete, note = fetch_fills(address, cursor_ms, end_ms,
                                                request=request, pause=pause)
            with connection:
                for fill in fills:
                    record_fill(connection, address, fill)
                if complete:
                    connection.execute('UPDATE wallets SET cursor_ms=?,last_checked_ms=? WHERE address=?',
                                       (end_ms, end_ms, address))
            fills_status = 'проверено' if complete else 'пробел'
        except Exception as exc:
            fills_status = 'пробел'
            note = type(exc).__name__
        dexes = [row[0] for row in connection.execute(
            'SELECT dex FROM dexes WHERE address=? ORDER BY dex', (address,))]
        if '' not in dexes:
            dexes.insert(0, '')
        try:
            checked_at = datetime.now(timezone.utc).isoformat()
            with connection:
                for dex in dexes:
                    for item in account_reader(address, dex):
                        position = item.get('position') or {}
                        coin = str(position.get('coin') or '')
                        if coin.upper() not in ('BTC', 'ETH'):
                            continue
                        connection.execute('''INSERT OR REPLACE INTO position_snapshots
                          (run_at,address,coin,signed_size,entry_price,liquidation_price,
                           unrealized_pnl,checked_at) VALUES(?,?,?,?,?,?,?,?)''',
                          (run_at,address,coin,str(position.get('szi') or ''),
                           str(position.get('entryPx') or ''),
                           str(position.get('liquidationPx') or ''),
                           str(position.get('unrealizedPnl') or ''),checked_at))
            positions_status = 'проверено'
        except Exception as exc:
            positions_status = 'пробел'
            note = (note + ', ' if note else '') + type(exc).__name__
        with connection:
            connection.execute('''INSERT OR REPLACE INTO checks
              (run_at,address,checked_at,fills_status,positions_status,fills_found,note)
              VALUES(?,?,?,?,?,?,?)''',
              (run_at,address,datetime.now(timezone.utc).isoformat(),fills_status,
               positions_status,len(fills),note))
        completed += 1
        if completed % 5 == 0 or completed == len(wallets):
            progress(f'История кошельков: {completed}/{len(wallets)} адресов')
        pause(1.2 + len(fills) * 0.004)
    return {'wallets': len(wallets), 'checked': completed,
            'gaps': connection.execute('SELECT COUNT(*) FROM checks WHERE run_at=? AND fills_status!=?',
                                       (run_at, 'проверено')).fetchone()[0]}


def report_rows(connection):
    connection.row_factory = sqlite3.Row
    events = [dict(row) for row in connection.execute(
        '''SELECT f.*,w.name FROM fills f JOIN wallets w ON w.address=f.address
           JOIN tracked_wallets t ON t.address=f.address
           WHERE UPPER(f.coin) IN ('BTC','ETH')
           ORDER BY f.time_ms DESC,f.event_id DESC''')]
    checks = [dict(row) for row in connection.execute(
        '''SELECT c.*,w.name FROM checks c JOIN wallets w ON w.address=c.address
           JOIN tracked_wallets t ON t.address=c.address
           ORDER BY c.run_at DESC,c.address''')]
    positions = [dict(row) for row in connection.execute(
        '''SELECT p.*,w.name FROM position_snapshots p JOIN wallets w ON w.address=p.address
           JOIN tracked_wallets t ON t.address=p.address
           WHERE UPPER(p.coin) IN ('BTC','ETH')
           ORDER BY p.run_at DESC,p.address,p.coin''')]
    return events, checks, positions

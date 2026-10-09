"""Telegram notifications for actual Bybit USDT-perpetual position transitions."""

import fcntl
import hashlib
import hmac
import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import websocket

from bybit_client import Client, pages

CONFIG_PATH = Path('/var/lib/my-agents/trader-api.json')
DB_PATH = Path('/var/lib/my-agents/trader-position-notifications.sqlite')
LOCK_PATH = Path('/var/lib/my-agents/trader-position-notifications.lock')
WS_URL = 'wss://stream.bybit.com/v5/private'


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('''CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT NOT NULL, position_idx INTEGER NOT NULL, side TEXT NOT NULL,
        size TEXT NOT NULL, updated_ms INTEGER NOT NULL,
        PRIMARY KEY (symbol, position_idx))''')
    db.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT, message TEXT NOT NULL,
        created_at INTEGER NOT NULL, sent_at INTEGER,
        close_event TEXT, next_try_at INTEGER NOT NULL DEFAULT 0)''')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(notifications)')}
    if 'close_event' not in columns:
        db.execute('ALTER TABLE notifications ADD COLUMN close_event TEXT')
    if 'next_try_at' not in columns:
        db.execute('ALTER TABLE notifications ADD COLUMN next_try_at INTEGER NOT NULL DEFAULT 0')
    db.execute('''CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
    db.commit()
    return db


def valid_position(raw):
    if raw.get('category') not in (None, 'linear'):
        return None
    symbol = str(raw.get('symbol') or '').upper()
    if not symbol.endswith('USDT') or not symbol[:-4].isalnum():
        return None
    idx = int(raw.get('positionIdx', -1))
    if idx not in (0, 1, 2):
        return None
    size = Decimal(str(raw.get('size') or '0'))
    if not size.is_finite() or size < 0:
        return None
    side = str(raw.get('side') or '')
    if side not in ('Buy', 'Sell', ''):
        return None
    updated_ms = int(raw.get('updatedTime') or 0)
    return {'symbol': symbol, 'position_idx': idx, 'side': side,
            'size': size, 'updated_ms': updated_ms,
            'entry_price': str(raw.get('entryPrice') or raw.get('avgPrice') or '')}


def direction(side, idx):
    return 'лонг' if side == 'Buy' or idx == 1 else 'шорт'


def apply_position(db, item, notify=True):
    key = (item['symbol'], item['position_idx'])
    old = db.execute('SELECT * FROM positions WHERE symbol=? AND position_idx=?', key).fetchone()
    old_size = Decimal(old['size']) if old else Decimal('0')
    new_size = item['size']
    if old and item['updated_ms'] and item['updated_ms'] < old['updated_ms']:
        return
    old_side = old['side'] if old else ''
    side = item['side'] or old_side
    message = None
    if notify and old_size == 0 and new_size > 0:
        message = (f'Открыт {direction(side, item["position_idx"])} {item["symbol"]}: '
                   f'{new_size} монет')
        if item['entry_price'] not in ('', '0'):
            message += f', средняя цена {item["entry_price"]} USDT'
        message += '.'
    elif notify and old_size > 0 and new_size == 0:
        message = f'Закрыт {direction(old_side, item["position_idx"])} {item["symbol"]}.'
    db.execute('''INSERT INTO positions (symbol, position_idx, side, size, updated_ms)
        VALUES (?, ?, ?, ?, ?) ON CONFLICT(symbol, position_idx) DO UPDATE SET
        side=excluded.side, size=excluded.size, updated_ms=excluded.updated_ms''',
        (item['symbol'], item['position_idx'], side, str(new_size),
         max(item['updated_ms'], old['updated_ms'] if old else 0)))
    if message:
        close_event = None
        if old_size > 0 and new_size == 0:
            close_event = json.dumps({'symbol': item['symbol'], 'side': old_side,
                                      'size': str(old_size), 'updated_ms': item['updated_ms'],
                                      'previous_ms': old['updated_ms']})
        db.execute('INSERT INTO notifications (message, created_at, close_event) VALUES (?, ?, ?)',
                   (message, int(time.time()), close_event))


def close_details(client, event):
    """Match the closing amount and side before attributing Bybit PnL to a position."""
    end = int(event['updated_ms']) + 60000
    start = max(0, int(event['previous_ms']) - 1000,
                int(event['updated_ms']) - 60000)
    if not start or not end:
        return None
    rows = pages(client, '/v5/position/closed-pnl',
                 {'category': 'linear', 'symbol': event['symbol'], 'startTime': start,
                  'endTime': end, 'limit': 100}, 'list')
    close_side = 'Buy' if event['side'] == 'Sell' else 'Sell'
    matched = []
    for row in rows:
        try:
            updated = int(row.get('updatedTime') or 0)
            size = Decimal(str(row.get('closedSize') or '0'))
            pnl = Decimal(str(row['closedPnl']))
            exit_price = Decimal(str(row['avgExitPrice']))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
        if (row.get('symbol') == event['symbol'] and row.get('side') == close_side
                and start <= updated <= end and size > 0 and pnl.is_finite()
                and exit_price.is_finite() and exit_price > 0):
            matched.append((updated, size, pnl, exit_price))
    # Starting with the latest closure avoids including a previous partial close.
    matched.sort(reverse=True)
    selected = []
    closed = Decimal('0')
    target = Decimal(event['size'])
    for row in matched:
        selected.append(row)
        closed += row[1]
        if closed >= target:
            break
    if closed != target:
        return None
    value = sum((row[1] * row[3] for row in selected), Decimal('0'))
    price = value / closed
    pnl = sum((row[2] for row in selected), Decimal('0'))
    executed_ms = max(row[0] for row in selected)
    date = datetime.fromtimestamp(executed_ms / 1000, ZoneInfo('Asia/Omsk'))
    pnl_text = f'+{pnl}' if pnl > 0 else str(pnl)
    return (f'Закрыт {direction(event["side"], 1 if event["side"] == "Buy" else 2)} '
            f'{event["symbol"]}. Цена закрытия: {price.normalize()} USDT. '
            f'PnL: {pnl_text} USDT. '
            f'Дата: {date:%d.%m.%Y %H:%M:%S} (Омск).')


def reconcile(client):
    rows = list(pages(client, '/v5/position/list',
                      {'category': 'linear', 'settleCoin': 'USDT', 'limit': 200}, 'list'))
    current = {}
    for raw in rows:
        try:
            item = valid_position(raw)
        except (ValueError, InvalidOperation, TypeError):
            continue
        if item is not None:
            current[(item['symbol'], item['position_idx'])] = item
    with connect() as db:
        initialized = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
        known = list(db.execute('SELECT * FROM positions'))
        if initialized:
            # The REST list may omit empty positions. Missing old positions are closed.
            for old in known:
                key = (old['symbol'], old['position_idx'])
                if key not in current and Decimal(old['size']) > 0:
                    current[key] = {'symbol': old['symbol'], 'position_idx': old['position_idx'],
                                    'side': old['side'], 'size': Decimal('0'),
                                    'updated_ms': int(time.time() * 1000), 'entry_price': ''}
        for item in current.values():
            apply_position(db, item, notify=bool(initialized))
        if not initialized:
            db.execute("INSERT INTO meta (key, value) VALUES ('initialized', '1')")


def flush_notifications():
    from telegram_bridge import send, state
    paired = state('trader')
    if not paired or not paired.get('owner_chat_id'):
        return
    with connect() as db:
        pending = list(db.execute('''SELECT id, message, created_at, close_event, next_try_at
            FROM notifications WHERE sent_at IS NULL ORDER BY id LIMIT 20'''))
    client = None
    for row in pending:
        message = row['message']
        if row['close_event']:
            if row['next_try_at'] > int(time.time()):
                continue
            try:
                if client is None:
                    cfg = json.loads(CONFIG_PATH.read_text())
                    client = Client(cfg['key'], cfg['secret'], cfg['region'])
                details = close_details(client, json.loads(row['close_event']))
            except Exception:
                details = None
            if details is None:
                if int(time.time()) - row['created_at'] < 300:
                    with connect() as db:
                        db.execute('UPDATE notifications SET next_try_at=? WHERE id=?',
                                   (int(time.time()) + 15, row['id']))
                    continue
                event = json.loads(row['close_event'])
                closed_at = datetime.fromtimestamp(
                    int(event['updated_ms']) / 1000, ZoneInfo('Asia/Omsk'))
                message += (f' Дата: {closed_at:%d.%m.%Y %H:%M:%S} (Омск). '
                            'Цена закрытия и PnL пока недоступны в Bybit.')
            else:
                message = details
        try:
            send(paired['token'], paired['owner_chat_id'], message)
        except Exception:
            return
        with connect() as db:
            db.execute('UPDATE notifications SET sent_at=? WHERE id=? AND sent_at IS NULL',
                       (int(time.time()), row['id']))


def handle_message(payload):
    if payload.get('topic') == 'order.linear':
        from trader_fill_notifications import queue_filled, flush
        items = payload.get('data') or []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    queue_filled(item)
            flush()
        return
    if payload.get('topic') != 'position.linear':
        return
    items = payload.get('data') or []
    if not isinstance(items, list):
        return
    with connect() as db:
        for raw in items:
            try:
                item = valid_position(raw)
            except (ValueError, InvalidOperation, TypeError):
                continue
            if item is not None:
                apply_position(db, item)
    flush_notifications()


def authenticate(ws, key, secret):
    expires = int((time.time() + 10) * 1000)
    signature = hmac.new(secret.encode(), f'GET/realtime{expires}'.encode(),
                         hashlib.sha256).hexdigest()
    ws.send(json.dumps({'op': 'auth', 'args': [key, expires, signature]}))
    response = json.loads(ws.recv())
    if response.get('op') != 'auth' or response.get('success') is not True:
        raise RuntimeError('Bybit private stream authentication failed')
    ws.send(json.dumps({'op': 'subscribe', 'args': ['position.linear', 'order.linear']}))


def run():
    while True:
        socket = None
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            client = Client(cfg['key'], cfg['secret'], cfg['region'])
            socket = websocket.create_connection(WS_URL, timeout=10)
            authenticate(socket, cfg['key'], cfg['secret'])
            reconcile(client)
            flush_notifications()
            from trader_fill_notifications import flush as flush_filled
            flush_filled()
            last_ping = 0
            while True:
                now = time.time()
                if now - last_ping >= 20:
                    socket.send(json.dumps({'op': 'ping'}))
                    last_ping = now
                try:
                    payload = json.loads(socket.recv())
                except websocket.WebSocketTimeoutException:
                    flush_notifications()
                    flush_filled()
                    continue
                handle_message(payload)
        except Exception as exc:
            print('Bybit position notifications reconnecting after ' +
                  type(exc).__name__, flush=True)
            time.sleep(5)
        finally:
            if socket is not None:
                socket.close()


if __name__ == '__main__':
    with open(LOCK_PATH, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        run()

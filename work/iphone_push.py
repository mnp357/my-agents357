"""Server-side iPhone alerts for connected Bybit accounts.

Secrets and APNs tokens stay in /var/lib/my-agents. This worker makes read-only
Bybit requests; it never submits orders or sends Telegram messages.
"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from bybit_client import Client, pages

ROOT = Path('/var/lib/my-agents')
ACCOUNTS = ROOT / 'iphone-accounts.json'
DATABASE = ROOT / 'iphone-push.sqlite'
APNS = ROOT / 'iphone-apns.json'
THREE_DAYS_MS = 3 * 24 * 60 * 60 * 1000


def save_accounts(accounts):
    temporary = ACCOUNTS.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(accounts, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, ACCOUNTS)


def load_accounts():
    return json.loads(ACCOUNTS.read_text()) if ACCOUNTS.exists() else {}


def connect():
    db = sqlite3.connect(DATABASE, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS devices (
            token TEXT PRIMARY KEY, uids TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS positions (
            uid TEXT NOT NULL, symbol TEXT NOT NULL, position_idx INTEGER NOT NULL,
            side TEXT NOT NULL, size TEXT NOT NULL,
            PRIMARY KEY(uid, symbol, position_idx));
        CREATE TABLE IF NOT EXISTS baselines (uid TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS events (
            token TEXT NOT NULL, event_key TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL, sent_at INTEGER, next_try_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(token, event_key));
    ''')
    return db


def register_device(token, uids):
    if not re.fullmatch(r'[a-fA-F0-9]{64,256}', token):
        raise ValueError('Некорректный токен iPhone.')
    uids = sorted(set(uids))
    if not uids or len(uids) > 50 or any(not re.fullmatch(r'[0-9]{1,20}', uid) for uid in uids):
        raise ValueError('Укажите UID аккаунтов из приложения.')
    with connect() as db:
        db.execute('INSERT INTO devices(token,uids,updated_at) VALUES(?,?,?) '
                   'ON CONFLICT(token) DO UPDATE SET uids=excluded.uids,updated_at=excluded.updated_at',
                   (token.lower(), json.dumps(uids), int(time.time())))
    return uids


def queue(db, uid, key, title, body):
    db.execute('INSERT OR IGNORE INTO events(token,event_key,title,body) VALUES(?,?,?,?)',
               ('telegram', uid + ':' + key, title, f'UID {uid} · {body}'))
    for device in db.execute('SELECT token,uids FROM devices'):
        if uid in json.loads(device['uids']):
            db.execute('INSERT OR IGNORE INTO events(token,event_key,title,body) VALUES(?,?,?,?)',
                       (device['token'], uid + ':' + key, title, body))


def current_positions(client):
    result = {}
    for row in pages(client, '/v5/position/list', {'category': 'linear', 'settleCoin': 'USDT', 'limit': 200}, 'list'):
        symbol = str(row.get('symbol') or '')
        if not symbol.endswith('USDT'):
            continue
        idx = int(row.get('positionIdx') or 0)
        size = str(row.get('size') or '0')
        if float(size) > 0:
            result[(symbol, idx)] = (str(row.get('side') or ''), size)
    return result


def current_orders(client):
    return list(pages(client, '/v5/order/realtime',
                      {'category': 'linear', 'settleCoin': 'USDT', 'openOnly': 0, 'limit': 50}, 'list'))


def scan_account(db, uid, config, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    client = Client(config['key'], config['secret'], config['region'])
    positions = current_positions(client)
    orders = current_orders(client)
    old = {(r['symbol'], r['position_idx']): (r['side'], r['size']) for r in
           db.execute('SELECT symbol,position_idx,side,size FROM positions WHERE uid=?', (uid,))}
    baseline = db.execute('SELECT 1 FROM baselines WHERE uid=?', (uid,)).fetchone()
    with db:
        if baseline:
            for identity in old.keys() | positions.keys():
                before = old.get(identity)
                after = positions.get(identity)
                symbol, idx = identity
                if before is None and after is not None:
                    side = 'Лонг' if after[0] == 'Buy' else 'Шорт'
                    queue(db, uid, f'open:{symbol}:{idx}:{now_ms}', 'Позиция открыта',
                          f'{symbol} · {side} · {after[1]}')
                elif before is not None and after is None:
                    side = 'Лонг' if before[0] == 'Buy' else 'Шорт'
                    queue(db, uid, f'close:{symbol}:{idx}:{now_ms}', 'Позиция закрыта',
                          f'{symbol} · {side}')
                elif before is not None and after is not None and before[0] != after[0]:
                    queue(db, uid, f'reverse-close:{symbol}:{idx}:{now_ms}', 'Позиция закрыта',
                          f'{symbol} · {before[0]}')
                    queue(db, uid, f'reverse-open:{symbol}:{idx}:{now_ms}', 'Позиция открыта',
                          f'{symbol} · {after[0]} · {after[1]}')
        db.execute('DELETE FROM positions WHERE uid=?', (uid,))
        db.executemany('INSERT INTO positions(uid,symbol,position_idx,side,size) VALUES(?,?,?,?,?)',
                       [(uid, symbol, idx, side, size)
                        for (symbol, idx), (side, size) in positions.items()])
        db.execute('INSERT OR IGNORE INTO baselines(uid) VALUES(?)', (uid,))
        for order in orders:
            if order.get('orderStatus') not in ('New', 'PartiallyFilled'):
                continue
            created = int(order.get('createdTime') or 0)
            if created <= 0 or created > now_ms:
                continue
            cycle = (now_ms - created) // THREE_DAYS_MS
            if cycle < 1:
                continue
            order_id = str(order.get('orderId') or '')
            if not order_id:
                continue
            symbol = str(order.get('symbol') or '')
            queue(db, uid, f'order:{order_id}:{cycle}', 'Ордер ожидает исполнения',
                  f'{symbol} · {cycle * 3} дн. · {order.get("side", "")} · {order.get("qty", "")}')


def send_pending(db):
    if not APNS.exists():
        return
    import httpx
    import jwt
    config = json.loads(APNS.read_text())
    private_key = Path(config['private_key_path']).read_text()
    timestamp = int(time.time())
    auth = jwt.encode({'iss': config['team_id'], 'iat': timestamp}, private_key,
                      algorithm='ES256', headers={'kid': config['key_id']})
    host = 'api.sandbox.push.apple.com' if config.get('environment') == 'sandbox' else 'api.push.apple.com'
    with httpx.Client(http2=True, timeout=15) as client:
        for event in db.execute('SELECT * FROM events WHERE token!=? AND sent_at IS NULL AND next_try_at<=? ORDER BY rowid LIMIT 100', ('telegram', timestamp)).fetchall():
            payload = {'aps': {'alert': {'title': event['title'], 'body': event['body']}, 'sound': 'default'}}
            try:
                response = client.post(f'https://{host}/3/device/{event["token"]}', json=payload,
                                       headers={'authorization': 'bearer ' + auth,
                                                'apns-topic': config['bundle_id'], 'apns-push-type': 'alert'})
            except httpx.HTTPError:
                response = None
            with db:
                if response is not None and response.status_code == 200:
                    db.execute('UPDATE events SET sent_at=? WHERE token=? AND event_key=?',
                               (timestamp, event['token'], event['event_key']))
                elif response is not None and response.status_code in (400, 410) and response.json().get('reason') in ('BadDeviceToken', 'Unregistered', 'DeviceTokenNotForTopic'):
                    db.execute('DELETE FROM devices WHERE token=?', (event['token'],))
                    db.execute('DELETE FROM events WHERE token=?', (event['token'],))
                else:
                    db.execute('UPDATE events SET next_try_at=? WHERE token=? AND event_key=?',
                               (timestamp + 300, event['token'], event['event_key']))


def send_telegram_pending(db):
    from telegram_bridge import send, state
    bot = state('trader')
    if not bot or bot['owner_chat_id'] is None:
        return
    timestamp = int(time.time())
    for event in db.execute('SELECT * FROM events WHERE token=? AND sent_at IS NULL AND next_try_at<=? ORDER BY rowid LIMIT 100',
                            ('telegram', timestamp)).fetchall():
        try:
            send(bot['token'], bot['owner_chat_id'], event['title'] + '\n' + event['body'])
        except Exception:
            with db:
                db.execute('UPDATE events SET next_try_at=? WHERE token=? AND event_key=?',
                           (timestamp + 300, 'telegram', event['event_key']))
        else:
            with db:
                db.execute('UPDATE events SET sent_at=? WHERE token=? AND event_key=?',
                           (timestamp, 'telegram', event['event_key']))


def run():
    import fcntl
    with open(ROOT / 'iphone-push.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with connect() as db:
            while True:
                for uid, account in load_accounts().items():
                    try:
                        scan_account(db, uid, account)
                    except Exception as exc:
                        print(f'Bybit scan failed for UID {uid}: {type(exc).__name__}', flush=True)
                try:
                    send_pending(db)
                except Exception as exc:
                    print(f'APNs delivery failed: {type(exc).__name__}', flush=True)
                try:
                    send_telegram_pending(db)
                except Exception as exc:
                    print(f'Telegram delivery failed: {type(exc).__name__}', flush=True)
                time.sleep(30)


if __name__ == '__main__':
    run()

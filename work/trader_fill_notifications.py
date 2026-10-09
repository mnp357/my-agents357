"""One concise Telegram notice per fully filled order placed by the trader bot."""

import json
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from trader_orders import db_connect


def _ensure_table(db):
    db.execute('''CREATE TABLE IF NOT EXISTS fill_notifications (
        code TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
        message TEXT NOT NULL, created_at INTEGER NOT NULL, sent_at INTEGER)''')


def _filled_message(code, params, item):
    try:
        qty = Decimal(str(item.get('cumExecQty') or '0'))
        price = Decimal(str(item.get('avgPrice') or '0'))
        if price <= 0 and qty > 0:
            price = Decimal(str(item.get('cumExecValue') or '0')) / qty
        updated_ms = int(item.get('updatedTime') or 0)
    except (ValueError, TypeError, InvalidOperation, ZeroDivisionError):
        return None
    if not qty.is_finite() or qty <= 0 or not price.is_finite() or price <= 0:
        return None
    if updated_ms <= 0:
        return None
    symbol = params.get('symbol')
    if item.get('symbol') != symbol or item.get('side') != params.get('side'):
        return None
    direction = 'Лонг' if params['side'] == 'Buy' else 'Шорт'
    occurred = datetime.fromtimestamp(updated_ms / 1000, ZoneInfo('Asia/Omsk'))
    price_text = format(price.normalize(), 'f')
    qty_text = format(qty.normalize(), 'f')
    return (f'Заявка {symbol} ({code}) исполнена.\n'
            f'{direction} · {qty_text} монет\n'
            f'Средняя цена исполнения: {price_text} USDT\n'
            f'Время исполнения: {occurred:%d.%m.%Y %H:%M} (Омск)')


def queue_filled(item):
    """Persist before sending; Bybit may push the same Filled event twice."""
    if item.get('orderStatus') != 'Filled':
        return False
    link_id = item.get('orderLinkId')
    if not link_id:
        return False
    with db_connect() as db:
        _ensure_table(db)
        row = db.execute('''SELECT code, owner_id, params_json FROM drafts
            WHERE order_link_id=? AND state IN ('submitting', 'accepted', 'unknown')''',
            (link_id,)).fetchone()
        if row is None:
            return False
        params = json.loads(row['params_json'])
        if params.get('orderType') != 'Limit':
            return False
        message = _filled_message(row['code'], params, item)
        if message is None:
            return False
        db.execute('''INSERT OR IGNORE INTO fill_notifications
            (code, owner_id, message, created_at) VALUES (?, ?, ?, ?)''',
            (row['code'], row['owner_id'], message, int(time.time())))
        return True


def flush():
    from telegram_bridge import send, state
    try:
        paired = state('trader')
    except (OSError, ValueError, sqlite3.Error):
        return
    if not paired or not paired.get('owner_chat_id'):
        return
    with db_connect() as db:
        _ensure_table(db)
        pending = list(db.execute('''SELECT code, owner_id, message FROM fill_notifications
            WHERE sent_at IS NULL ORDER BY created_at LIMIT 20'''))
    for row in pending:
        if row['owner_id'] != paired['owner_id']:
            continue
        try:
            send(paired['token'], paired['owner_chat_id'], row['message'])
        except Exception:
            return
        with db_connect() as db:
            db.execute('''UPDATE fill_notifications SET sent_at=?
                WHERE code=? AND sent_at IS NULL''', (int(time.time()), row['code']))

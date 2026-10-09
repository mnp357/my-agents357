"""One-time Telegram confirmation for user-requested Bybit limit orders."""

import json
import os
import secrets
import sqlite3
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from bybit_client import BybitError, BybitUncertainError
from trader_preview import PreviewError, format_preview, preview_open
from trader_close import format_close, preview_close

DB_PATH = Path('/var/lib/my-agents/trader-intents.sqlite')
CONFIRM_SECONDS = 600
ORDER_REVIEW_SECONDS = 5 * 24 * 60 * 60
CONTROL_CHECK_SECONDS = 3 * 60 * 60


def db_connect():
    if not DB_PATH.exists():
        fd = os.open(DB_PATH, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('''CREATE TABLE IF NOT EXISTS drafts (
        code TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        intent_json TEXT NOT NULL,
        params_json TEXT NOT NULL,
        position_json TEXT NOT NULL DEFAULT '[]',
        existing_count INTEGER NOT NULL,
        leverage TEXT NOT NULL,
        state TEXT NOT NULL,
        order_link_id TEXT NOT NULL UNIQUE,
        order_id TEXT,
        error TEXT
    )''')
    db.execute('''CREATE TABLE IF NOT EXISTS ladder_drafts (
        code TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
        child_codes_json TEXT NOT NULL, state TEXT NOT NULL,
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(drafts)')}
    if 'position_json' not in columns:
        db.execute("ALTER TABLE drafts ADD COLUMN position_json TEXT NOT NULL DEFAULT '[]'")
    for name, definition in (
        ('submitted_at', 'INTEGER'),
        ('expiry_at', 'INTEGER'),
        ('expiry_state', 'TEXT'),
        ('expiry_next_check_at', 'INTEGER'),
        ('expiry_attempted_at', 'INTEGER'),
        ('expiry_message', 'TEXT'),
        ('expiry_notified_at', 'INTEGER'),
        ('expiry_prompt_message_id', 'INTEGER'),
        ('control_price', 'TEXT'),
        ('control_direction', 'TEXT'),
        ('control_state', 'TEXT'),
        ('control_last_price', 'TEXT'),
        ('control_last_seen_at', 'INTEGER'),
        ('control_next_check_at', 'INTEGER'),
        ('control_prompt_message_id', 'INTEGER'),
    ):
        if name not in columns:
            db.execute('ALTER TABLE drafts ADD COLUMN ' + name + ' ' + definition)
    db.commit()
    return db


def encoded_intent(intent):
    if intent.get('kind') == 'close':
        return json.dumps({'kind': 'close', 'symbol': intent['symbol'],
                           'position_side': intent['position_side']})
    return json.dumps({'symbol': intent['symbol'], 'side': intent['side'],
                       'price': str(intent['price']),
                       'percent': str(intent['percent']) if intent['percent'] is not None else None,
                       'amount_usdt': str(intent.get('amount_usdt')) if intent.get('amount_usdt') is not None else None,
                       'control_price': str(intent.get('control_price')) if intent.get('control_price') is not None else None,
                       'take_profit': str(intent.get('take_profit')) if intent.get('take_profit') is not None else None})


def decoded_intent(data):
    raw = json.loads(data)
    if raw.get('kind') == 'close':
        return {'kind': 'close', 'symbol': raw['symbol'],
                'position_side': raw['position_side']}
    return {'symbol': raw['symbol'], 'side': raw['side'], 'price': Decimal(raw['price']),
            'percent': Decimal(raw['percent']) if raw['percent'] is not None else None,
            'amount_usdt': Decimal(raw['amount_usdt']) if raw.get('amount_usdt') is not None else None,
            'control_price': Decimal(raw['control_price']) if raw.get('control_price') is not None else None,
            'take_profit': Decimal(raw['take_profit']) if raw.get('take_profit') is not None else None}


def create_draft(owner_id, intent, preview, supersede=True):
    code = secrets.token_urlsafe(9)
    link_id = 'ma' + secrets.token_hex(16)
    now = int(time.time())
    with db_connect() as db:
        if supersede:
            db.execute("UPDATE drafts SET state='superseded' WHERE owner_id=? AND state='pending'", (owner_id,))
            db.execute("UPDATE ladder_drafts SET state='superseded' WHERE owner_id=? AND state='pending'", (owner_id,))
        db.execute('''INSERT INTO drafts
            (code, owner_id, created_at, expires_at, intent_json, params_json,
             position_json, existing_count, leverage, state, order_link_id,
             control_price, control_direction, control_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)''',
            (code, owner_id, now, now + CONFIRM_SECONDS, encoded_intent(intent),
             json.dumps(preview['params'], sort_keys=True),
             json.dumps(preview['position_snapshot'], sort_keys=True), preview['existing_count'],
             str(preview['leverage']), link_id,
             str(intent.get('control_price')) if intent.get('control_price') is not None else None,
             preview.get('watch_direction'),
             'pending' if intent.get('control_price') is not None else None))
    description = (format_close(preview) if intent.get('kind') == 'close'
                   else format_preview(preview))
    return (description +
        '\n\nЕсли всё верно, в течение 10 минут отправьте: /confirm ' + code +
        '\nДля отмены черновика: /cancel')


def create_ladder_draft(owner_id, intent, previews):
    if len(previews) != 4:
        raise PreviewError('Лесенка должна содержать четыре заявки.')
    child_codes = []
    for index, preview in enumerate(previews):
        child_intent = dict(intent, ladder=False, price=Decimal(preview['params']['price']))
        if index == 3:
            if intent.get('amount_usdt') is not None:
                child_intent['amount_usdt'] = intent['amount_usdt'] * 2
            else:
                child_intent['percent'] = intent['percent'] * 2
        reply = create_draft(owner_id, child_intent, preview,
                             supersede=index == 0)
        child_codes.append(reply.split('/confirm ', 1)[1].splitlines()[0])
    group_code = secrets.token_urlsafe(9)
    now = int(time.time())
    with db_connect() as db:
        db.execute('''INSERT INTO ladder_drafts
            (code, owner_id, child_codes_json, state, created_at, expires_at)
            VALUES (?, ?, ?, 'pending', ?, ?)''',
            (group_code, owner_id, json.dumps(child_codes), now, now + CONFIRM_SECONDS))
    first = previews[0]['params']
    direction = 'Лонг' if first['side'] == 'Buy' else 'Шорт'
    lines = [f'{direction} {first["symbol"]} лесенкой — 4 заявки:']
    for index, preview in enumerate(previews, 1):
        params = preview['params']
        lines.append(f'{index}. {params["price"]} USDT · {params["qty"]} монет '
                     f'≈ {preview["notional"]} USDT' + (' (двойной объём)' if index == 4 else ''))
    total = sum((item['notional'] for item in previews), Decimal('0'))
    lines.append(f'Всего: ≈ {total} USDT.')
    if previews[0].get('current_liquidation'):
        lines.append('Ликвидация уже открытой позиции: ' +
                     str(previews[0]['current_liquidation']) +
                     ' USDT. После исполнения лесенки она изменится.')
    elif previews[0].get('ladder_liquidation') == 'outside_range':
        lines.append('Ликвидация при исполнении всех 4 заявок: вне диапазона цены контракта.')
    elif previews[0].get('ladder_liquidation'):
        lines.append('Ликвидация при исполнении всех 4 заявок: ≈ ' +
                     str(previews[0]['ladder_liquidation']) + ' USDT.')
    else:
        lines.append('Ликвидацию после исполнения определит Bybit.')
    if intent.get('control_price') is not None:
        lines.append(f'Контрольная цена / тейк-профит: {intent["control_price"]} USDT')
    lines.append('Ордера не отправлены. Одно подтверждение отправит все четыре заявки; '
                 'если Bybit примет только часть, сообщу результат каждой.')
    return ('\n'.join(lines) +
            '\n\nЕсли всё верно, в течение 10 минут отправьте: /confirm ' + group_code +
            '\nДля отмены черновика: /cancel')


def set_state(code, state, order_id=None, error=None):
    with db_connect() as db:
        db.execute('UPDATE drafts SET state=?, order_id=?, error=? WHERE code=?',
                   (state, order_id, error, code))


def confirm_ladder(owner_id, code, client, expected_uid):
    try:
        if client.verify_trade_subaccount()['uid'] != expected_uid:
            return 'UID ключа изменился. Лесенка не отправлена.'
    except BybitError as exc:
        return 'Лесенка не отправлена: ' + str(exc)
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM ladder_drafts WHERE code=? AND owner_id=?',
                         (code, owner_id)).fetchone()
        if row is None:
            return 'Черновик лесенки не найден.'
        if row['state'] != 'pending':
            return 'Эта лесенка уже обработана (' + row['state'] + '). Повторной отправки не будет.'
        if int(time.time()) > row['expires_at']:
            db.execute("UPDATE ladder_drafts SET state='expired' WHERE code=?", (code,))
            return 'Срок подтверждения лесенки истёк. Заявки не отправлены.'
        children = json.loads(row['child_codes_json'])
        db.execute("UPDATE ladder_drafts SET state='submitting' WHERE code=?", (code,))
    lines = ['Результат лесенки:']
    accepted = 0
    for index, child in enumerate(children, 1):
        result = confirm(owner_id, child, client, expected_uid)
        with db_connect() as db:
            child_row = db.execute('SELECT state, params_json FROM drafts WHERE code=?',
                                   (child,)).fetchone()
        state = child_row['state'] if child_row else 'unknown'
        price = json.loads(child_row['params_json'])['price'] if child_row else '—'
        lines.append(f'{index}. {price} USDT — ' +
                     ('принята Bybit' if state == 'accepted' else result))
        if state != 'accepted':
            break
        accepted += 1
    if accepted < len(children):
        with db_connect() as db:
            db.executemany("UPDATE drafts SET state='cancelled' WHERE code=? AND state='pending'",
                           [(child,) for child in children[accepted + 1:]])
        lines.append(f'Следующие заявки не отправлены. Bybit подтвердил приём: {accepted} из 4.')
    else:
        lines.append('Bybit принял все 4 заявки. Исполнение проверяйте отдельно.')
    with db_connect() as db:
        db.execute('UPDATE ladder_drafts SET state=? WHERE code=?',
                   ('accepted' if accepted == 4 else 'partial' if accepted else 'failed', code))
    return '\n'.join(lines)


def confirm(owner_id, code, client, expected_uid):
    with db_connect() as db:
        group = db.execute('SELECT 1 FROM ladder_drafts WHERE code=? AND owner_id=?',
                           (code, owner_id)).fetchone()
    if group:
        return confirm_ladder(owner_id, code, client, expected_uid)
    now = int(time.time())
    db = db_connect()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM drafts WHERE code=? AND owner_id=?', (code, owner_id)).fetchone()
        if row is None:
            db.rollback()
            return 'Код заявки не найден. Отправьте новое поручение.'
        if row['state'] != 'pending':
            db.rollback()
            if row['state'] == 'accepted':
                return 'Эта заявка уже отправлена на Bybit. Повторной отправки не будет.'
            return 'Эта заявка уже не ожидает подтверждения (' + row['state'] + '). Повторной отправки не будет.'
        if now > row['expires_at']:
            db.execute("UPDATE drafts SET state='expired' WHERE code=?", (code,))
            db.commit()
            return 'Срок подтверждения истёк. Отправьте новое поручение для свежего расчёта.'
        db.execute("UPDATE drafts SET state='checking' WHERE code=? AND state='pending'", (code,))
        db.commit()
    finally:
        db.close()
    try:
        intent = decoded_intent(row['intent_json'])
        if intent.get('kind') == 'close':
            actual = preview_close(client, intent, expected_uid)
            if (json.dumps(actual['params'], sort_keys=True) != row['params_json'] or
                    json.dumps(actual['position_snapshot'], sort_keys=True) != row['position_json']):
                set_state(code, 'changed')
                return 'Позиция изменилась. Заявка на закрытие не отправлена. Подготовьте её заново.'
        payload = json.loads(row['params_json'])
        if intent.get('kind') != 'close' and (
                payload.get('orderType') != 'Limit' or
                payload.get('positionIdx') != (1 if intent['side'] == 'Buy' else 2) or
                payload.get('symbol') != intent['symbol'] or
                payload.get('side') != intent['side'] or
                payload.get('price') != str(intent['price'])):
            set_state(code, 'invalid')
            return 'Сохранённые параметры заявки не совпали с поручением. Ордер не отправлен.'
        if intent.get('kind') != 'close' and intent.get('take_profit') is not None and (
                payload.get('takeProfit') != str(intent['take_profit']) or
                payload.get('tpTriggerBy') != 'LastPrice' or
                payload.get('tpslMode') != 'Partial' or
                payload.get('tpOrderType') != 'Market'):
            set_state(code, 'invalid')
            return 'Сохранённый тейк-профит не совпал с поручением. Ордер не отправлен.'
        if intent.get('kind') != 'close' and intent.get('control_price') is not None:
            if (row['control_price'] != str(intent['control_price']) or
                    row['control_direction'] not in ('up', 'down') or
                    payload.get('takeProfit') != str(intent['control_price'])):
                set_state(code, 'invalid')
                return 'Контрольная цена и тейк-профит в черновике не совпали. Подготовьте новую заявку.'
            tickers = client.get('/v5/market/tickers',
                                 {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
            if len(tickers) != 1 or tickers[0].get('symbol') != intent['symbol']:
                raise PreviewError('Не удалось проверить контрольную цену перед отправкой.')
            last_price = Decimal(str(tickers[0].get('lastPrice')))
            reached = (last_price >= intent['control_price'] if row['control_direction'] == 'up'
                       else last_price <= intent['control_price'])
            if reached:
                set_state(code, 'cancelled')
                return 'Контрольный уровень уже достигнут. Заявка не отправлена.'
    except (PreviewError, BybitError) as exc:
        set_state(code, 'invalid', error=str(exc))
        return 'Заявка не отправлена: ' + str(exc)
    except Exception:
        set_state(code, 'invalid')
        return 'Не удалось повторно проверить заявку. Она не отправлена.'
    payload['orderLinkId'] = row['order_link_id']
    # Persist the irreversible transition before calling the exchange.
    submitted_at = int(time.time())
    is_limit = payload['orderType'] == 'Limit'
    with db_connect() as db:
        changed = db.execute("""UPDATE drafts SET state='submitting', submitted_at=?,
            expiry_at=?, expiry_state=?, expiry_next_check_at=?, control_state=?, control_next_check_at=?
            WHERE code=? AND state='checking'""",
            (submitted_at, None,
             'scheduled' if is_limit else None,
             submitted_at + ORDER_REVIEW_SECONDS if is_limit else None,
             'active' if row['control_price'] else None,
             submitted_at + CONTROL_CHECK_SECONDS if row['control_price'] else None, code))
        if changed.rowcount != 1:
            return 'Состояние заявки изменилось. Повторной отправки не будет.'
    try:
        result = client.post_once('/v5/order/create', payload)
    except BybitUncertainError:
        set_state(code, 'unknown')
        return ('Ответ Bybit не получен. Повторно заявку не отправляю. '
                'Проверьте ордер на Bybit по идентификатору ' + row['order_link_id'] + '.')
    except BybitError as exc:
        set_state(code, 'rejected', error=str(exc))
        return str(exc) + ' Новая заявка не создана.'
    except Exception:
        set_state(code, 'unknown')
        return ('Результат отправки неизвестен. Повторно заявку не отправляю. '
                'Проверьте ордер на Bybit по идентификатору ' + row['order_link_id'] + '.')
    order_id = result.get('orderId')
    if not order_id:
        set_state(code, 'unknown')
        return ('Bybit не вернул ID ордера. Повторно заявку не отправляю. '
                'Проверьте её по идентификатору ' + row['order_link_id'] + '.')
    set_state(code, 'accepted', order_id=str(order_id))
    summary = ('на закрытие позиции ' + payload['symbol'] + ' по рынку'
               if payload['orderType'] == 'Market' else
               ('лонг' if payload['side'] == 'Buy' else 'шорт') + ' ' +
               payload['symbol'] + ' по ' + payload['price'] + ' USDT')
    return ('Bybit принял заявку ' + summary + ', количество ' + payload['qty'] +
            '.')


def cancel(owner_id):
    with db_connect() as db:
        changed = db.execute("UPDATE drafts SET state='cancelled' WHERE owner_id=? AND state='pending'", (owner_id,))
        groups = db.execute("UPDATE ladder_drafts SET state='cancelled' WHERE owner_id=? AND state='pending'", (owner_id,))
    return ('Черновик отменён. Ордер не отправлен.' if changed.rowcount or groups.rowcount else
            'Черновика, ожидающего подтверждения, нет. Уже отправленные ордера это не отменяет.')


def cancel_code(owner_id, code):
    with db_connect() as db:
        group = db.execute('SELECT child_codes_json FROM ladder_drafts WHERE owner_id=? AND code=? AND state=?',
                           (owner_id, code, 'pending')).fetchone()
        if group:
            db.execute("UPDATE ladder_drafts SET state='cancelled' WHERE code=?", (code,))
            db.executemany("UPDATE drafts SET state='cancelled' WHERE code=? AND state='pending'",
                           [(child,) for child in json.loads(group['child_codes_json'])])
            return 'Не подтверждено. Все четыре заявки не отправлены.'
        changed = db.execute("UPDATE drafts SET state='cancelled' WHERE owner_id=? AND code=? AND state='pending'",
                             (owner_id, code))
    return ('Не подтверждено. Ордер не отправлен.' if changed.rowcount else
            'Эта заявка уже не ожидает подтверждения.')


def order_status(owner_id, code, client):
    with db_connect() as db:
        row = db.execute('SELECT * FROM drafts WHERE code=? AND owner_id=?',
                         (code, owner_id)).fetchone()
    if row is None:
        return 'Код заявки не найден.'
    if row['state'] in ('pending', 'expired', 'cancelled', 'superseded', 'changed', 'invalid'):
        return 'Состояние черновика: ' + row['state'] + '. Этот черновик не отправлен на Bybit.'
    if row['state'] == 'rejected':
        return 'Bybit отклонил заявку: ' + str(row['error'] or 'причина не указана')
    symbol = json.loads(row['params_json'])['symbol']
    try:
        result = client.get('/v5/order/realtime',
                            {'category': 'linear', 'symbol': symbol,
                             'orderLinkId': row['order_link_id']})
    except BybitError:
        return 'Не удалось прочитать статус Bybit. Проверьте ордер непосредственно на бирже.'
    matches = [item for item in (result.get('list') or [])
               if item.get('orderLinkId') == row['order_link_id']]
    if len(matches) != 1:
        return ('Заявка не найдена в краткой истории Bybit. Это не доказывает, что она не была отправлена. '
                'Проверьте её на бирже по идентификатору ' + row['order_link_id'] + '.')
    item = matches[0]
    expiry_note = ''
    if row['expiry_next_check_at']:
        deadline = datetime.fromtimestamp(row['expiry_next_check_at'], ZoneInfo('Asia/Omsk')).strftime(
            '%d.%m.%Y %H:%M (Омск)')
        if row['expiry_state'] in ('scheduled', 'awaiting_decision', 'review_retry'):
            expiry_note = ' Следующая проверка заявки: ' + deadline + '. Автоотмены нет.'
        elif row['expiry_state'] == 'cancel_sent':
            expiry_note = ' Отмена отправлена; ожидается подтверждение Bybit.'
        elif row['expiry_message']:
            expiry_note = ' ' + row['expiry_message']
    liquidation_note = ''
    try:
        if Decimal(str(item.get('cumExecQty') or '0')) > 0:
            params = json.loads(row['params_json'])
            positions = client.get('/v5/position/list',
                                   {'category': 'linear', 'symbol': symbol}).get('list') or []
            matching = [position for position in positions
                        if int(position.get('positionIdx', -1)) == int(params['positionIdx'])
                        and Decimal(str(position.get('size') or '0')) > 0]
            if len(matching) == 1 and matching[0].get('liqPrice'):
                liquidation_note = (' Цена ликвидации открытой позиции по Bybit: ' +
                                    str(matching[0]['liqPrice']) + ' USDT.')
            else:
                liquidation_note = ' Bybit пока не показывает цену ликвидации открытой позиции.'
    except (BybitError, ValueError, KeyError, TypeError, ArithmeticError):
        liquidation_note = ' Цену ликвидации сейчас не удалось получить из Bybit.'
    return ('Bybit: ' + str(item.get('orderStatus') or 'статус не указан') +
            '. Исполнено: ' + str(item.get('cumExecQty') or '0') +
            ' из ' + str(item.get('qty') or '—') +
            '. Средняя цена: ' + str(item.get('avgPrice') or '—') +
            '. ID: ' + str(item.get('orderId') or '—') + '.' + expiry_note + liquidation_note)

"""Send reminders every five days for still-open limit orders."""

import fcntl
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bybit_client import BybitError, BybitUncertainError, Client
from trader_orders import ORDER_REVIEW_SECONDS, db_connect

CONFIG_PATH = Path('/var/lib/my-agents/trader-api.json')
LOCK_PATH = Path('/var/lib/my-agents/trader-expiry.lock')
ACTIVE = {'New', 'PartiallyFilled'}
TERMINAL = {'Filled', 'Cancelled', 'PartiallyFilledCanceled', 'Rejected', 'Deactivated'}
CANCEL_STATES = {'cancel_requested', 'cancel_sent', 'cancel_retry'}


def russian_days(value):
    value = int(value)
    if value % 10 == 1 and value % 100 != 11:
        word = 'день'
    elif value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        word = 'дня'
    else:
        word = 'дней'
    return f'{value} {word}'


def update_expiry(code, **fields):
    with db_connect() as db:
        db.execute('UPDATE drafts SET ' + ', '.join(k + '=?' for k in fields) +
                   ' WHERE code=?', (*fields.values(), code))


def due_orders(now):
    with db_connect() as db:
        return [dict(row) for row in db.execute('''SELECT * FROM drafts
            WHERE expiry_next_check_at <= ? AND state IN ('accepted', 'unknown')
              AND expiry_state IN ('scheduled', 'awaiting_decision', 'review_retry',
                                   'cancel_requested', 'cancel_sent', 'cancel_retry')
            ORDER BY expiry_next_check_at LIMIT 50''', (now,))]


def retry(row, now, cancelling, message):
    update_expiry(row['code'], expiry_state='cancel_retry' if cancelling else 'review_retry',
                  expiry_next_check_at=now + (60 if cancelling else 300),
                  expiry_message=message, expiry_notified_at=None)


def finish(row, message, state='done'):
    update_expiry(row['code'], expiry_state=state, expiry_next_check_at=None,
                  expiry_message=message, expiry_notified_at=None,
                  expiry_prompt_message_id=None)


def order_from_bybit(client, row, params):
    query = {'category': 'linear', 'symbol': params['symbol'],
             'orderLinkId': row['order_link_id']}
    result = client.get('/v5/order/realtime', query)
    matches = [item for item in (result.get('list') or [])
               if item.get('orderLinkId') == row['order_link_id']]
    if not matches:
        result = client.get('/v5/order/history', query)
        matches = [item for item in (result.get('list') or [])
                   if item.get('orderLinkId') == row['order_link_id']]
    return matches[0] if len(matches) == 1 else None


def prompt_owner(row, item, now):
    from telegram_bridge import send, state
    paired = state('trader')
    if (not paired or paired.get('owner_id') != row['owner_id'] or
            not paired.get('owner_chat_id')):
        retry(row, now, False, 'Не удалось связаться с владельцем заявки.')
        return
    opened = datetime.fromtimestamp(row['submitted_at'], ZoneInfo('Asia/Omsk')).strftime(
        '%d.%m.%Y в %H:%M (Омск)')
    params = json.loads(row['params_json'])
    executed = str(item.get('cumExecQty') or '0')
    qty = str(item.get('qty') or params['qty'])
    remaining = str(item.get('leavesQty') or '—')
    text = (f'Напоминание: заявка {params["symbol"]} по {params["price"]} USDT открыта {opened}. '
            f'Она ещё не исполнена полностью: исполнено {executed} из {qty}, '
            f'осталось {remaining}.')
    open_days = max(0, (now - row['submitted_at']) // 86400)
    if open_days >= 5:
        text += f' Заявка открыта уже {russian_days(open_days)}.'
    if row['control_price'] and row['control_state'] == 'active':
        text += (f' Контрольный уровень {row["control_price"]} USDT '
                 'пока не зафиксирован ботом. Заявка остаётся открытой.')
    try:
        send(paired['token'], paired['owner_chat_id'], text)
    except Exception:
        retry(row, now, False, 'Не удалось отправить вопрос о заявке в Telegram.')
        return
    update_expiry(row['code'], expiry_state='scheduled',
                  expiry_next_check_at=now + ORDER_REVIEW_SECONDS,
                  expiry_prompt_message_id=None,
                  expiry_message=None, expiry_notified_at=None)


def process_one(client, row, now):
    params = json.loads(row['params_json'])
    symbol = params['symbol']
    label = symbol + ' (' + row['order_link_id'] + ')'
    cancelling = row['expiry_state'] in CANCEL_STATES
    try:
        item = order_from_bybit(client, row, params)
    except BybitError:
        retry(row, now, cancelling, 'Не удалось проверить заявку ' + label + ' на Bybit.')
        return
    if item is None:
        retry(row, now, cancelling, 'Заявка ' + label + ' не найдена в ответе Bybit. Проверьте её на бирже.')
        return
    if (item.get('symbol') != symbol or item.get('orderType') != 'Limit' or
            item.get('side') != params['side'] or
            (row['order_id'] and item.get('orderId') != row['order_id'])):
        finish(row, 'Данные заявки ' + label + ' не совпали с сохранёнными. Проверьте её на Bybit.', 'review')
        return
    status = item.get('orderStatus')
    executed = str(item.get('cumExecQty') or '0')
    qty = str(item.get('qty') or params['qty'])
    if status in TERMINAL:
        if status == 'Filled':
            from trader_fill_notifications import queue_filled, flush
            queued = queue_filled(item)
            finish(row, None if queued else
                   'Заявка ' + symbol + ' (' + row['code'] + ') исполнена. '
                   'Bybit не передал полные сведения о цене или времени; проверьте детали на бирже.')
            flush()
        else:
            finish(row, 'Заявка ' + label + ': Bybit показывает ' + str(status) +
                   '. Исполнено ' + executed + ' из ' + qty + '.')
        return
    if status not in ACTIVE or str(item.get('leavesQty') or '') == '0':
        retry(row, now, cancelling, 'Статус заявки ' + label + ' пока не позволяет определить открытый остаток.')
        return
    if not cancelling:
        if row['control_price'] and row['control_state'] != 'active':
            update_expiry(row['code'], expiry_next_check_at=now + 300)
            return
        prompt_owner(row, item, now)
        return
    # The owner has pressed Delete. Re-read status before every retry.
    update_expiry(row['code'], expiry_state='cancel_sent',
                  expiry_next_check_at=now + 60, expiry_attempted_at=now,
                  expiry_message=None)
    try:
        client.post_once('/v5/order/cancel',
                         {'category': 'linear', 'symbol': symbol,
                          'orderLinkId': row['order_link_id']})
    except (BybitError, BybitUncertainError):
        retry(row, now, True, 'Отмена остатка заявки ' + label + ' пока не подтверждена Bybit.')


def choose_order(owner_id, code, message_id, keep, now=None, client=None, expected_uid=None):
    now = int(time.time()) if now is None else now
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM drafts WHERE code=? AND owner_id=?',
                         (code, owner_id)).fetchone()
        if (row is None or row['state'] not in ('accepted', 'unknown') or
                row['expiry_state'] != 'awaiting_decision' or
                row['expiry_prompt_message_id'] != message_id):
            return 'Этот вопрос уже не актуален. Проверьте статус заявки на Bybit.'
        if keep:
            db.execute('''UPDATE drafts SET expiry_state='scheduled',
                expiry_next_check_at=?, expiry_prompt_message_id=NULL,
                expiry_message=NULL WHERE code=?''', (now + ORDER_REVIEW_SECONDS, code))
            return 'Оставил заявку открытой. Напомню о ней снова через 5 дней.'
        db.execute('''UPDATE drafts SET expiry_state='cancel_requested',
            expiry_next_check_at=?, expiry_prompt_message_id=NULL,
            expiry_message=NULL WHERE code=?''', (now, code))
    try:
        if client is None:
            cfg = json.loads(CONFIG_PATH.read_text())
            client = Client(cfg['key'], cfg['secret'], cfg['region'])
            expected_uid = cfg['uid']
        if client.verify_trade_subaccount()['uid'] != expected_uid:
            raise BybitError('UID субаккаунта изменился.')
        with db_connect() as db:
            current = dict(db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone())
        process_one(client, current, now)
    except (OSError, ValueError, KeyError, BybitError):
        retry({'code': code}, now, True, 'Не удалось сразу проверить запрос на отмену. Повторю проверку.')
    return 'Проверяю заявку на Bybit и отменяю только её неисполненный остаток. Сообщу результат.'


def notify_pending():
    from telegram_bridge import send, state
    paired = state('trader')
    if not paired or not paired.get('owner_chat_id'):
        return
    with db_connect() as db:
        rows = [dict(row) for row in db.execute('''SELECT code, owner_id, expiry_message
            FROM drafts WHERE expiry_message IS NOT NULL AND expiry_notified_at IS NULL
            AND expiry_state IN ('cancel_retry', 'done', 'review') LIMIT 20''')]
    for row in rows:
        if row['owner_id'] != paired['owner_id']:
            continue
        try:
            send(paired['token'], paired['owner_chat_id'], row['expiry_message'])
        except Exception:
            return
        update_expiry(row['code'], expiry_notified_at=int(time.time()))


def run_once(now=None, client=None, expected_uid=None):
    now = int(time.time()) if now is None else now
    from trader_control import due as due_controls, run_due as run_control_checks
    controls_due = bool(due_controls(now))
    if controls_due and client is None:
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            client = Client(cfg['key'], cfg['secret'], cfg['region'])
            expected_uid = cfg['uid']
        except (OSError, ValueError, KeyError):
            client = None
    if controls_due and client is not None:
        run_control_checks(client, now)
    rows = due_orders(now)
    if rows:
        try:
            if client is None:
                cfg = json.loads(CONFIG_PATH.read_text())
                client = Client(cfg['key'], cfg['secret'], cfg['region'])
                expected_uid = cfg['uid']
            if client.verify_trade_subaccount()['uid'] != expected_uid:
                raise BybitError('UID субаккаунта изменился.')
        except (OSError, ValueError, KeyError, BybitError):
            for row in rows:
                retry(row, now, row['expiry_state'] in CANCEL_STATES,
                      'Не удалось проверить торговый субаккаунт для заявки ' + row['order_link_id'] + '.')
        else:
            for row in rows:
                try:
                    process_one(client, row, now)
                except Exception:
                    retry(row, now, row['expiry_state'] in CANCEL_STATES,
                          'Ошибка проверки заявки ' + row['order_link_id'] + '.')
    notify_pending()


if __name__ == '__main__':
    with open(LOCK_PATH, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        run_once()

"""Check Bybit trade-price history every three hours for exact control levels."""

import json
import time
from decimal import Decimal, InvalidOperation

from bybit_client import BybitError, BybitUncertainError
from trader_orders import CONTROL_CHECK_SECONDS, db_connect

ACTIVE = {'New', 'PartiallyFilled'}
TERMINAL = {'Filled', 'Cancelled', 'PartiallyFilledCanceled', 'Rejected', 'Deactivated'}


def update(code, **fields):
    with db_connect() as db:
        db.execute('UPDATE drafts SET ' + ', '.join(key + '=?' for key in fields) +
                   ' WHERE code=?', (*fields.values(), code))


def due(now):
    with db_connect() as db:
        return [dict(row) for row in db.execute('''SELECT * FROM drafts
            WHERE control_price IS NOT NULL AND state IN ('accepted', 'unknown')
              AND control_state IN ('active', 'triggered', 'cancel_requested', 'cancel_sent')
              AND control_next_check_at <= ? ORDER BY control_next_check_at LIMIT 50''', (now,))]


def crossed(client, row, now):
    """Use exact high/low from one-minute Bybit traded-price candles, not snapshots."""
    params = json.loads(row['params_json'])
    start = int(row['control_last_seen_at'] or row['submitted_at'])
    level = Decimal(row['control_price'])
    direction = row['control_direction']
    if direction not in ('up', 'down'):
        raise ValueError('Invalid control direction')
    # Bybit returns at most 1000 one-minute candles per request. Cover outages too.
    cursor = (start // 60) * 60 * 1000
    end = now * 1000
    while cursor <= end:
        chunk_end = min(end, cursor + 999 * 60 * 1000)
        candles = client.get('/v5/market/kline',
                             {'category': 'linear', 'symbol': params['symbol'],
                              'interval': '1', 'start': cursor, 'end': chunk_end,
                              'limit': 1000}).get('list') or []
        if not candles:
            raise BybitError('Bybit не вернул историю цены.')
        for candle in candles:
            if len(candle) < 5:
                raise BybitError('Bybit вернул неполную свечу.')
            stamp = int(candle[0])
            if stamp < cursor or stamp > chunk_end:
                continue
            edge = Decimal(str(candle[2] if direction == 'up' else candle[3]))
            if not edge.is_finite() or edge <= 0:
                raise BybitError('Bybit вернул некорректную цену.')
            if (edge >= level if direction == 'up' else edge <= level):
                return True, str(edge)
        cursor = chunk_end + 60 * 1000
    return False, None


def tell_owner(row, message):
    from telegram_bridge import send, state
    try:
        paired = state('trader')
        if paired and paired.get('owner_id') == row['owner_id'] and paired.get('owner_chat_id'):
            send(paired['token'], paired['owner_chat_id'], message)
    except Exception:
        pass


def inspect_order(client, row):
    from trader_expiry import order_from_bybit
    params = json.loads(row['params_json'])
    item = order_from_bybit(client, row, params)
    if item is None:
        return None
    if (item.get('symbol') != params['symbol'] or item.get('side') != params['side'] or
            item.get('orderType') != 'Limit' or
            (row['order_id'] and item.get('orderId') != row['order_id'])):
        raise BybitError('Данные заявки не совпали с сохранённым поручением.')
    return item


def finalize(row, status, item):
    update(row['code'], control_state='done', control_next_check_at=None,
           control_prompt_message_id=None)
    if row['control_state'] in ('cancel_requested', 'cancel_sent'):
        params = json.loads(row['params_json'])
        tell_owner(row, f'Контрольная цена {row["control_price"]} USDT достигнута. '
                   f'Итог заявки {params["symbol"]}: {status}. Исполнено '
                   f'{item.get("cumExecQty") or "0"} из {item.get("qty") or "—"}. '
                   'Уже открытую позицию бот не закрывал.')


def prompt_owner(row, item, now):
    from telegram_bridge import send, state
    paired = state('trader')
    if not paired or paired.get('owner_id') != row['owner_id'] or not paired.get('owner_chat_id'):
        update(row['code'], control_next_check_at=now + CONTROL_CHECK_SECONDS)
        return
    params = json.loads(row['params_json'])
    text = (f'Цена {params["symbol"]} достигла контрольного уровня '
            f'{row["control_price"]} USDT. Лимитная заявка ещё открыта: '
            f'исполнено {item.get("cumExecQty") or "0"} из '
            f'{item.get("qty") or params["qty"]}. '
            'Тейк-профит для уже исполненной части остаётся на Bybit. '
            'Отменить только неисполненный остаток заявки?')
    markup = {'inline_keyboard': [[
        {'text': 'Отменить остаток', 'callback_data': 'ctrlcancel:' + row['code']},
        {'text': 'Оставить заявку', 'callback_data': 'ctrlkeep:' + row['code']},
    ]]}
    try:
        message = send(paired['token'], paired['owner_chat_id'], text, markup)
        message_id = message['message_id']
    except Exception:
        update(row['code'], control_next_check_at=now + CONTROL_CHECK_SECONDS)
        return
    update(row['code'], control_state='awaiting_decision',
           control_prompt_message_id=message_id, control_next_check_at=None)


def choose_order(owner_id, code, message_id, cancel, client=None, now=None):
    now = int(time.time()) if now is None else now
    with db_connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM drafts WHERE code=? AND owner_id=?',
                         (code, owner_id)).fetchone()
        if (row is None or row['state'] not in ('accepted', 'unknown') or
                row['control_state'] != 'awaiting_decision' or
                row['control_prompt_message_id'] != message_id):
            return 'Этот вопрос уже не актуален. Проверьте заявку на Bybit.'
        if not cancel:
            db.execute('''UPDATE drafts SET control_state='kept',
                control_next_check_at=NULL, control_prompt_message_id=NULL
                WHERE code=?''', (code,))
            return 'Оставил заявку. Повторных предложений отменить её по этому уровню не будет.'
        db.execute('''UPDATE drafts SET control_state='cancel_requested',
            control_next_check_at=?, control_prompt_message_id=NULL
            WHERE code=?''', (now, code))
    if client is None:
        from pathlib import Path
        from bybit_client import Client
        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
        client = Client(cfg['key'], cfg['secret'], cfg['region'])
    with db_connect() as db:
        current = dict(db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone())
    try:
        process_one(client, current, now)
    except (BybitError, ValueError, InvalidOperation, KeyError, TypeError):
        update(code, control_next_check_at=now + CONTROL_CHECK_SECONDS)
        return 'Запрос на отмену сохранён, но проверить его на Bybit сейчас не удалось. Проверьте заявку на бирже.'
    with db_connect() as db:
        final_state = db.execute('SELECT control_state FROM drafts WHERE code=?',
                                 (code,)).fetchone()[0]
    if final_state == 'done':
        return 'Проверил заявку: на Bybit уже нет открытого остатка. Новую отмену не отправлял.'
    if final_state == 'cancel_sent':
        return 'По вашему подтверждению запросил на Bybit отмену только неисполненного остатка. Принятие запроса ещё не означает завершение отмены.'
    return 'Запрос на отмену сохранён. Состояние заявки пока неясно; проверьте её на Bybit.'


def process_one(client, row, now):
    params = json.loads(row['params_json'])
    if row['control_state'] == 'active':
        hit, edge = crossed(client, row, now)
        if not hit:
            update(row['code'], control_last_seen_at=now,
                   control_next_check_at=now + CONTROL_CHECK_SECONDS)
            return
        update(row['code'], control_state='triggered', control_last_price=edge,
               control_last_seen_at=now, control_next_check_at=now + CONTROL_CHECK_SECONDS)
        row['control_state'] = 'triggered'
    item = inspect_order(client, row)
    if item is None:
        update(row['code'], control_next_check_at=now + CONTROL_CHECK_SECONDS)
        return
    status = item.get('orderStatus')
    if status in TERMINAL:
        finalize(row, status, item)
        return
    if status not in ACTIVE or Decimal(str(item.get('leavesQty') or '0')) <= 0:
        update(row['code'], control_next_check_at=now + CONTROL_CHECK_SECONDS)
        return
    if row['control_state'] == 'triggered':
        prompt_owner(row, item, now)
        return
    # The owner pressed Cancel. Cancel only this exact order; keep any filled position.
    update(row['code'], control_state='cancel_sent',
           control_next_check_at=now + CONTROL_CHECK_SECONDS)
    try:
        client.post_once('/v5/order/cancel',
                         {'category': 'linear', 'symbol': params['symbol'],
                          'orderLinkId': row['order_link_id']})
    except (BybitError, BybitUncertainError):
        tell_owner(row, 'Контрольный уровень достигнут, но Bybit пока не подтвердил отмену '
                   'остатка заявки. Проверьте её на бирже.')


def run_due(client, now=None):
    now = int(time.time()) if now is None else now
    for row in due(now):
        try:
            process_one(client, row, now)
        except (BybitError, ValueError, InvalidOperation, KeyError, TypeError):
            update(row['code'], control_next_check_at=now + CONTROL_CHECK_SECONDS)
            tell_owner(row, 'Не удалось проверить контрольную цену или заявку на Bybit. '
                       'Проверьте ордер на бирже; повторная автоматическая проверка через три часа.')

"""Read-only preview for closing one entire USDT perpetual position."""

import re
from decimal import Decimal

from trader_preview import PreviewError, decimal_text, number


def parse_close(text):
    if not re.search(r'(?:^/close\b|\bзакрыть\b|\bзакрой\b)', text, re.I):
        raise PreviewError('Напишите: «Закрыть лонг ADA полностью по рынку».')
    match = re.search(r'\b(лонг|long|шорт|short)\b\s+([A-Za-z0-9]{2,20})\b', text, re.I)
    if not match:
        raise PreviewError('Укажите, какую позицию закрыть: «лонг ADA» или «шорт ADA».')
    if not re.search(r'\b(полностью|всю|весь|all)\b', text, re.I):
        raise PreviewError('Сейчас поддерживается только полное закрытие. Добавьте слово «полностью».')
    if not re.search(r'\b(по\s+рынку|рыночн\w*|market)\b', text, re.I):
        raise PreviewError('Добавьте «по рынку» — лимитное закрытие пока не настроено.')
    symbol = match.group(2).upper()
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    if not re.fullmatch(r'[A-Z0-9]{1,20}USDT', symbol):
        raise PreviewError('Пока поддерживаются бессрочные фьючерсы к USDT.')
    return {'kind': 'close', 'symbol': symbol,
            'position_side': 'Buy' if match.group(1).lower() in ('лонг', 'long') else 'Sell'}


def preview_close(client, intent, expected_uid):
    identity = client.verify_trade_subaccount()
    if identity['uid'] != expected_uid:
        raise PreviewError('UID ключа изменился. Переподключите субаккаунт.')
    instruments = client.get('/v5/market/instruments-info',
                             {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
    if len(instruments) != 1:
        raise PreviewError('Bybit не вернул параметры указанного контракта.')
    instrument = instruments[0]
    if (instrument.get('symbol') != intent['symbol'] or
            instrument.get('contractType') != 'LinearPerpetual' or
            instrument.get('settleCoin') != 'USDT'):
        raise PreviewError('Этот инструмент не является бессрочным USDT-фьючерсом.')
    positions = client.get('/v5/position/list',
                           {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
    active = [row for row in positions if row.get('side') == intent['position_side']
              and number_or_zero(row.get('size')) > 0]
    if len(active) != 1:
        raise PreviewError('Не найдена ровно одна открытая позиция указанной стороны.')
    row = active[0]
    qty = number(row.get('size'))
    maximum = number((instrument.get('lotSizeFilter') or {}).get('maxMktOrderQty'))
    if qty > maximum:
        raise PreviewError('Позиция больше максимального размера одной рыночной заявки Bybit; автоматическое закрытие не выполняется.')
    idx = int(row.get('positionIdx'))
    if idx not in (0, 1, 2):
        raise PreviewError('Не удалось определить режим позиции на Bybit.')
    if (idx == 1 and intent['position_side'] != 'Buy') or (idx == 2 and intent['position_side'] != 'Sell'):
        raise PreviewError('Сторона позиции не соответствует её режиму на Bybit.')
    close_side = 'Sell' if intent['position_side'] == 'Buy' else 'Buy'
    mark = row.get('markPrice') or ''
    params = {'category': 'linear', 'symbol': intent['symbol'], 'side': close_side,
              'orderType': 'Market', 'qty': decimal_text(qty),
              'positionIdx': idx, 'reduceOnly': True}
    return {'params': params, 'existing_count': len(active),
            'leverage': row.get('leverage') or 'не указано Bybit',
            'position_snapshot': sorted((str(pos.get('positionIdx')), str(pos.get('side')),
                                         str(pos.get('size'))) for pos in positions
                                        if number_or_zero(pos.get('size')) > 0),
            'mark_price': mark, 'position_side': intent['position_side']}


def number_or_zero(value):
    try:
        result = Decimal(str(value or '0'))
    except Exception:
        raise PreviewError('Bybit вернул некорректный размер позиции.') from None
    if not result.is_finite() or result < 0:
        raise PreviewError('Bybit вернул некорректный размер позиции.')
    return result


def format_close(preview):
    p = preview['params']
    direction = 'лонг' if preview['position_side'] == 'Buy' else 'шорт'
    return (f'Закрыть {direction} {p["symbol"]} полностью\n'
            f'Рыночная заявка, количество: {p["qty"]} монет\n'
            f'Текущая цена маркировки: {preview["mark_price"] or "не указана"} USDT\n'
            f'Плечо позиции: {preview["leverage"]}\n'
            'Заявка только уменьшает позицию (reduce-only). Фактическая цена исполнения может отличаться.\n'
            'Ордер пока не отправлен.')

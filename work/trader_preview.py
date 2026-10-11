"""Read-only preview of a single USDT perpetual limit order.

This module has no order-submission method. A preview is never queued for later.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from bybit_client import BybitError

class PreviewError(Exception):
    pass


def number(value):
    try:
        result = Decimal(str(value).replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise PreviewError('Не удалось прочитать число в поручении.') from None
    if not result.is_finite() or result <= 0:
        raise PreviewError('Цена и размер должны быть положительными числами.')
    return result


ASSET_ALIASES = (
    (r'\b(?:биткоин|биткойн)[а-я]*\b', 'BTC'),
    (r'\b(?:эфир|этериум)[а-я]*\b', 'ETH'),
    (r'\bсолан[а-я]*\b', 'SOL'),
    (r'\b(?:рипл|иксрп)[а-я]*\b', 'XRP'),
    (r'\b(?:кардано|ад[аеуы])\b', 'ADA'),
    (r'\bдодж[а-я]*\b', 'DOGE'),
    (r'\bлайткоин[а-я]*\b', 'LTC'),
    (r'\bхайп[а-я]*\b', 'HYPE'),
)


def order_symbol(text, direction_end):
    """Read either a ticker or a common Russian asset name after the direction."""
    before_price = re.split(r'\b(?:точка\s+входа|цена\s+входа|цена|limit)\b',
                            text[direction_end:], maxsplit=1, flags=re.I)[0]
    explicit = re.search(r'\b([A-Za-z][A-Za-z0-9]{1,19})\b', before_price)
    if explicit:
        symbol = explicit.group(1).upper()
        return {'SOLANO': 'SOL', 'SOLANA': 'SOL'}.get(symbol, symbol)
    for pattern, symbol in ASSET_ALIASES:
        if re.search(pattern, before_price, re.I):
            return symbol
    return None


def parse_open(text):
    """Recognize only an explicit open-long/open-short limit order request."""
    # Voice transcription often groups thousands as "2 443" or "84 357".
    text = re.sub(r'(?<=\d)[\s\u00a0](?=\d{3}\b)', '', text)
    if not re.search(r'(?:^/preview\b|\bоткрыть\b|\bоткрой\b|\bпостав(?:ь|ить)\s+ордер\b|^/open\b)', text, re.I):
        raise PreviewError('Для расчёта напишите: «Открыть лонг ADA, цена 0.25, размер 1% капитала».')
    direction = re.search(r'\b(лонг|long|шорт|short)\b', text, re.I)
    if not direction:
        raise PreviewError('Укажите направление и монету: «лонг ADA» или «шорт ADAUSDT».')
    side = 'Buy' if direction.group(1).lower() in ('лонг', 'long') else 'Sell'
    symbol = order_symbol(text, direction.end())
    if symbol is None:
        raise PreviewError('Укажите монету после направления: например, «шорт ETH» или «шорт по эфиру».')
    if not symbol.endswith('USDT'):
        symbol += 'USDT'
    if not re.fullmatch(r'[A-Z0-9]{1,20}USDT', symbol):
        raise PreviewError('Пока поддерживаются бессрочные фьючерсы к USDT.')
    price_match = re.search(r'(?:точка\s+входа|цена\s+входа|цена|по|limit)\s*[:=]?\s*(\d+(?:[.,]\d+)?)', text, re.I)
    if not price_match:
        # Compact command: /preview long ADAUSDT 0.25 1%
        price_match = re.search(r'\b(?:лонг|long|шорт|short)\s+[A-Za-z0-9]{2,20}\s+(\d+(?:[.,]\d+)?)', text, re.I)
    if not price_match:
        raise PreviewError('Укажите цену лимитной заявки: «цена 0.25».')
    percent_match = re.search(r'(\d+(?:[.,]\d+)?)\s*%', text)
    amount_match = re.search(
        r'\bразмер(?:\s+позиции)?\s*(?:на|:|=)?\s*(\d+(?:[.,]\d+)?)\s*'
        r'(?:USDT|USD|доллар(?:ов|а)?)\b', text, re.I)
    if bool(percent_match) == bool(amount_match):
        raise PreviewError('Укажите один размер позиции: «размер 1% капитала» или «размер позиции на 10 USDT».')
    price = number(price_match.group(1))
    if re.match(r'\s*(?:тыс\.|тыс\b|тысяч[а-я]*\b)', text[price_match.end():], re.I):
        price *= 1000
    percent = number(percent_match.group(1)) if percent_match else None
    amount_usdt = number(amount_match.group(1)) if amount_match else None
    control_match = re.search(r'(?:контроль(?:н(?:ая|ый|ую)?\s+(?:цен[а-я]*|уров[а-я]*))?|уровень\s+отмены)\s*[:=]?\s*(\d+(?:[.,]\d+)?)', text, re.I)
    tp_match = re.search(r'(?:take[ -]?profit|тейк[ -]?профит|тейк)\s*[:=]?\s*(?:по\s+(?:цене\s*)?)?(\d+(?:[.,]\d+)?)', text, re.I)
    control_price = number(control_match.group(1)) if control_match else None
    take_profit = number(tp_match.group(1)) if tp_match else None
    if control_price is not None and take_profit is not None and control_price != take_profit:
        raise PreviewError('Контрольная цена и тейк-профит должны быть одной и той же ценой.')
    shared_price = control_price if control_price is not None else take_profit
    control_price = shared_price
    take_profit = shared_price
    if take_profit is not None and ((side == 'Buy' and take_profit <= price) or
                                    (side == 'Sell' and take_profit >= price)):
        raise PreviewError('Контрольная цена должна быть выше входа для лонга и ниже входа для шорта.')
    return {'symbol': symbol, 'side': side, 'price': price,
            'percent': percent, 'amount_usdt': amount_usdt,
            'control_price': control_price, 'take_profit': take_profit,
            'ladder': bool(re.search(r'\bлесенк\w*', text, re.I))}


def exact_step(value, step):
    return value % step == 0


def decimal_text(value):
    return format(value, 'f')


def nonnegative_number(value):
    try:
        result = Decimal(str(value or '0'))
    except (InvalidOperation, ValueError):
        raise PreviewError('Bybit вернул некорректный размер позиции.') from None
    if not result.is_finite() or result < 0:
        raise PreviewError('Bybit вернул некорректный размер позиции.')
    return result


def estimate_liquidation(client, intent, available, notional, qty, tick, price_filter):
    """Approximate an unhedged cross-margin liquidation level before execution."""
    try:
        if client.get('/v5/account/info').get('marginMode') != 'REGULAR_MARGIN':
            return None
        available = nonnegative_number(available)
        tiers = client.get('/v5/market/risk-limit',
                           {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
        tier = next((row for row in sorted(tiers, key=lambda row: Decimal(str(row['riskLimitValue'])))
                     if notional <= Decimal(str(row['riskLimitValue']))), None)
        if not tier:
            return None
        maintenance = max(Decimal('0'),
                          notional * Decimal(str(tier['maintenanceMargin'])) -
                          Decimal(str(tier.get('mmDeduction') or '0')))
        cushion = available - maintenance
        if cushion <= 0:
            return None
        raw = (intent['price'] - cushion / qty if intent['side'] == 'Buy'
               else intent['price'] + cushion / qty)
        if raw <= 0 or raw < number(price_filter.get('minPrice')) or raw > number(price_filter.get('maxPrice')):
            return 'outside_range'
        rounded = (raw / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        return decimal_text(rounded)
    except (BybitError, PreviewError, InvalidOperation, ValueError, TypeError, KeyError, StopIteration):
        return None


def preview_open(client, intent, expected_uid):
    # Opening always targets the hedge side. Bybit rejects positionIdx 1/2 if
    # that contract has been switched to one-way mode; no mode audit is needed.
    wallet = client.get('/v5/account/wallet-balance', {'accountType': 'UNIFIED'}).get('list') or []
    if len(wallet) != 1 or wallet[0].get('accountType') != 'UNIFIED':
        raise PreviewError('Не удалось определить капитал торгового субаккаунта.')
    equity = number(wallet[0].get('totalEquity') or '0')
    available = wallet[0].get('totalAvailableBalance') or ''
    instruments = client.get('/v5/market/instruments-info',
                             {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
    if len(instruments) != 1:
        raise PreviewError('Bybit не вернул параметры указанного контракта.')
    instrument = instruments[0]
    if (instrument.get('symbol') != intent['symbol'] or
            instrument.get('contractType') != 'LinearPerpetual' or
            instrument.get('settleCoin') != 'USDT' or
            instrument.get('status') != 'Trading'):
        raise PreviewError('Этот инструмент сейчас не является торгуемым бессрочным USDT-фьючерсом.')
    price_filter = instrument.get('priceFilter') or {}
    lot_filter = instrument.get('lotSizeFilter') or {}
    tick = number(price_filter.get('tickSize'))
    step = number(lot_filter.get('qtyStep'))
    price = intent['price']
    if not exact_step(price, tick):
        raise PreviewError('Цена не соответствует шагу Bybit: ' + decimal_text(tick))
    if price < number(price_filter.get('minPrice')) or price > number(price_filter.get('maxPrice')):
        raise PreviewError('Цена вне допустимого диапазона Bybit.')
    control_price = intent.get('control_price')
    if control_price is not None and (not exact_step(control_price, tick) or
            control_price < number(price_filter.get('minPrice')) or
            control_price > number(price_filter.get('maxPrice'))):
        raise PreviewError('Контрольная цена не соответствует шагу или диапазону цены Bybit.')
    requested_notional = (intent['amount_usdt'] if intent.get('amount_usdt') is not None
                          else equity * intent['percent'] / Decimal('100'))
    qty = (requested_notional / price / step).to_integral_value(rounding=ROUND_DOWN) * step
    if qty < number(lot_filter.get('minOrderQty')):
        raise PreviewError('Размер меньше минимального количества Bybit для этого контракта.')
    if qty > number(lot_filter.get('maxOrderQty')):
        raise PreviewError('Размер больше максимального количества одной заявки на Bybit.')
    actual_notional = qty * price
    try:
        min_notional = Decimal(str(lot_filter.get('minNotionalValue') or '0'))
    except InvalidOperation:
        raise PreviewError('Bybit вернул некорректный минимальный размер заявки.') from None
    if not min_notional.is_finite() or min_notional < 0:
        raise PreviewError('Bybit вернул некорректный минимальный размер заявки.')
    if actual_notional < min_notional:
        raise PreviewError('Стоимость заявки ниже минимума Bybit для этого контракта.')
    position_idx = 1 if intent['side'] == 'Buy' else 2
    try:
        positions = client.get('/v5/position/list',
                               {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
    except BybitError:
        positions = None
    existing = ([row for row in positions if nonnegative_number(row.get('size')) > 0]
                if positions is not None else [])
    same_side = [row for row in existing if int(row.get('positionIdx', -1)) == position_idx]
    position_snapshot = sorted((str(row.get('positionIdx')), str(row.get('side')),
                                str(row.get('size'))) for row in existing)
    current_liquidation = next((row.get('liqPrice') for row in same_side
                                if row.get('liqPrice')), None)
    liquidation_estimate = (estimate_liquidation(client, intent, available, actual_notional,
                                                qty, tick, price_filter)
                            if positions is not None and not existing else None)
    params = {'category': 'linear', 'symbol': intent['symbol'], 'side': intent['side'],
              'orderType': 'Limit', 'qty': decimal_text(qty), 'price': decimal_text(price),
              'timeInForce': 'GTC', 'positionIdx': position_idx}
    if intent.get('take_profit') is not None:
        params.update({'takeProfit': decimal_text(intent['take_profit']),
                       'tpTriggerBy': 'LastPrice', 'tpslMode': 'Partial',
                       'tpOrderType': 'Market'})
    watch_direction = None
    last_price = None
    if intent.get('control_price') is not None:
        tickers = client.get('/v5/market/tickers',
                             {'category': 'linear', 'symbol': intent['symbol']}).get('list') or []
        if len(tickers) != 1 or tickers[0].get('symbol') != intent['symbol']:
            raise PreviewError('Не удалось получить текущую цену для контрольного уровня.')
        last_price = number(tickers[0].get('lastPrice'))
        if intent['control_price'] == last_price:
            raise PreviewError('Контрольный уровень уже равен текущей цене. Укажите другой уровень.')
        watch_direction = 'up' if intent['control_price'] > last_price else 'down'
    return {'params': params, 'equity': equity, 'available': available,
            'tick': tick, 'price_filter': price_filter,
            'percent': intent['percent'], 'amount_usdt': intent.get('amount_usdt'),
            'notional': actual_notional,
            'leverage': 'не проверялось', 'existing_count': len(existing),
            'position_snapshot': position_snapshot,
            'current_liquidation': current_liquidation,
            'liquidation_estimate': liquidation_estimate,
            'control_price': intent.get('control_price'),
            'watch_direction': watch_direction, 'last_price': last_price}


def preview_ladder(client, intent, expected_uid):
    """Four independent orders; the last uses twice the requested USDT notional."""
    first = preview_open(client, intent, expected_uid)
    tick = first['tick']
    previews = [first]
    for step_number in (1, 2, 3):
        offset = Decimal(step_number) / Decimal('100')
        factor = Decimal('1') - offset if intent['side'] == 'Buy' else Decimal('1') + offset
        raw_price = intent['price'] * factor
        price = (raw_price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        if price <= 0 or price == Decimal(previews[-1]['params']['price']):
            raise PreviewError('Шаг цены Bybit не позволяет разместить четыре разные ступени.')
        level_intent = dict(intent, price=price, ladder=False)
        if step_number == 3:
            if intent.get('amount_usdt') is not None:
                level_intent['amount_usdt'] = intent['amount_usdt'] * 2
            else:
                level_intent['percent'] = intent['percent'] * 2
        previews.append(preview_open(client, level_intent, expected_uid))
    if len({Decimal(item['params']['price']) for item in previews}) != 4:
        raise PreviewError('Шаг цены Bybit не позволяет разместить четыре разные ступени.')
    if first['existing_count'] == 0:
        total_qty = sum((Decimal(item['params']['qty']) for item in previews), Decimal('0'))
        total_notional = sum((item['notional'] for item in previews), Decimal('0'))
        combined_intent = dict(intent, price=total_notional / total_qty)
        first['ladder_liquidation'] = estimate_liquidation(
            client, combined_intent, first['available'], total_notional,
            total_qty, tick, first['price_filter'])
    return previews


def format_preview(preview):
    p = preview['params']
    direction = 'Лонг' if p['side'] == 'Buy' else 'Шорт'
    size_note = (f'задано {decimal_text(preview["amount_usdt"])} USDT полной стоимости'
                 if preview.get('amount_usdt') is not None else
                 f'{decimal_text(preview["percent"])}% капитала')
    if preview.get('current_liquidation'):
        liquidation_line = (f'Текущая цена ликвидации уже открытой позиции по Bybit: '
                            f'{preview["current_liquidation"]} USDT. После исполнения новой заявки она изменится.\n')
    elif preview.get('liquidation_estimate') == 'outside_range':
        liquidation_line = ('Оценка цены ликвидации: вне доступного диапазона цены контракта '
                            'при текущем свободном балансе. После исполнения Bybit пересчитает риск.\n')
    elif preview.get('liquidation_estimate'):
        liquidation_line = ('Ликвидация: ≈ ' +
                            preview['liquidation_estimate'] +
                            ' USDT\n')
    else:
        liquidation_line = 'Цена ликвидации новой позиции: Bybit определит после исполнения заявки.\n'
    control_line = ('Контрольная цена / тейк-профит: ' + decimal_text(preview['control_price']) +
                    ' USDT\n'
                    if preview.get('control_price') is not None else '')
    return (f'{direction} {p["symbol"]}\n'
            f'Вход: {p["price"]} USDT\n'
            f'{control_line}'
            f'Размер: {p["qty"]} монет ≈ {decimal_text(preview["notional"])} USDT '
            f'({size_note})\n'
            f'Капитал субаккаунта: {decimal_text(preview["equity"])} USD\n'
            f'{liquidation_line}\n'
            'Ордер не отправлен.')

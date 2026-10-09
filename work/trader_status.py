"""Read-only account-wide status for the Bybit trading subaccount."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bybit_client import BybitError, pages


def _positive(value):
    try:
        number = Decimal(str(value or '0'))
    except (InvalidOperation, TypeError):
        return False
    return number.is_finite() and number > 0


def _direction(side):
    return {'Buy': 'лонг', 'Sell': 'шорт'}.get(side, 'сторона неизвестна')


def _action(side):
    return {'Buy': 'покупка', 'Sell': 'продажа'}.get(side, 'сторона неизвестна')


def _position_line(row):
    symbol = row.get('symbol') or '—'
    size = row.get('size') or '—'
    entry = row.get('avgPrice') or row.get('entryPrice') or '—'
    raw_pnl = row.get('unrealisedPnl')
    if raw_pnl in (None, ''):
        pnl = '—'
    else:
        try:
            amount = Decimal(str(raw_pnl))
            pnl = (f'+{amount}' if amount > 0 else str(amount)) if amount.is_finite() else '—'
        except (InvalidOperation, TypeError):
            pnl = '—'
    return f'{symbol} · {_direction(row.get("side"))} · {size} монет · вход {entry} USDT · PnL {pnl} USDT'


def _order_line(row):
    symbol = row.get('symbol') or '—'
    remaining = row.get('leavesQty') or row.get('qty') or '—'
    price = row.get('price') or '—'
    order_type = row.get('orderType') or ''
    label = 'по рынку' if order_type == 'Market' else f'по {price} USDT'
    trigger = row.get('triggerPrice')
    if trigger and trigger != '0':
        label += f', триггер {trigger}'
    if row.get('reduceOnly') is True:
        label += ', закрытие'
    return f'{symbol} · {_action(row.get("side"))} · осталось {remaining} · {label}'


def _section(title, rows, render, limit=12):
    lines = [f'{title}: {len(rows)}']
    if rows:
        lines.extend('• ' + render(row) for row in rows[:limit])
        if len(rows) > limit:
            lines.append(f'Ещё {len(rows) - limit} — уточните монету: /status BTC.')
    return '\n'.join(lines)


def account_status(client, expected_uid, symbol=None):
    if client.verify_trade_subaccount()['uid'] != expected_uid:
        raise BybitError('UID ключа отличается от подключённого субаккаунта.')
    params = {'category': 'linear', 'limit': 200}
    params.update({'symbol': symbol} if symbol else {'settleCoin': 'USDT'})
    positions = [row for row in pages(client, '/v5/position/list', params, 'list')
                 if _positive(row.get('size'))]
    order_params = {'category': 'linear', 'openOnly': 0, 'limit': 50}
    order_params.update({'symbol': symbol} if symbol else {'settleCoin': 'USDT'})
    orders = list(pages(client, '/v5/order/realtime', order_params, 'list'))
    positions.sort(key=lambda row: (row.get('symbol') or '', row.get('positionIdx', 0)))
    orders.sort(key=lambda row: row.get('symbol') or '')
    checked = datetime.now(ZoneInfo('Asia/Omsk')).strftime('%d.%m.%Y %H:%M')
    heading = f'Статус Bybit · {symbol or "все USDT-фьючерсы"}\nПроверено {checked} (Омск)'
    return '\n\n'.join((heading,
        _section('Открытые позиции', positions, _position_line),
        _section('Активные ордера', orders, _order_line)))

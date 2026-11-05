"""Keep the seven largest long and short positions from every run."""
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from liquidation_report import analyse


PER_SIDE = 7


def wallet_name(address):
    if not address:
        return 'Адрес недоступен'
    return f'Кошелёк {address[2:6].upper()}…{address[-4:].upper()}'


def quantity(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def snapshot(data):
    positions, coverage = analyse(data)
    observations = []
    for side in ('Лонг', 'Шорт'):
        largest = sorted((row for row in positions if row[1] == side and
                          str(row[0]).upper() in ('BTC', 'ETH')),
                         key=lambda row: -row[6])[:PER_SIDE]
        for rank, row in enumerate(largest, 1):
            wallet = row[8]
            observations.append({
                'side': side, 'rank': rank, 'wallet': wallet,
                'name': wallet_name(wallet), 'symbol': row[0],
                'size_usd': row[6], 'quantity': row[9],
                'entry_price': row[3], 'liquidation_price': row[4],
                'observed_at': row[7],
            })
    return {
        'run_at': data['started_at'],
        'complete': not data.get('cancelled') and all(row[4] == 'Проверено' for row in coverage),
        'observations': observations,
    }


def load_history(path):
    path = Path(path)
    if not path.exists():
        return {'version': 1, 'runs': []}
    history = json.loads(path.read_text())
    if history.get('version') != 1 or not isinstance(history.get('runs'), list):
        raise ValueError('Некорректный файл истории кошельков')
    return history


def append_history(history, data):
    current = snapshot(data)
    if not current['observations']:
        return history
    runs = list(history['runs'])
    if any(run['run_at'] == current['run_at'] for run in runs):
        return history
    previous = runs[-1]['observations'] if runs else []
    previous_by_position = {
        (item['wallet'], item['symbol'], item['side']): item
        for item in previous if item.get('wallet')
    }
    for item in current['observations']:
        old = previous_by_position.get((item['wallet'], item['symbol'], item['side'])) if item['wallet'] else None
        before = quantity(old.get('quantity')) if old else None
        after = quantity(item.get('quantity'))
        item['quantity_change'] = None
        if not runs:
            item['change'] = 'Первое наблюдение'
        elif old is None:
            item['change'] = 'Не было в прошлом топ-7'
        elif before is None or after is None:
            item['change'] = 'Количество не сравнить'
        else:
            delta = after - before
            item['quantity_change'] = str(delta)
            item['change'] = ('Количество выросло' if delta > 0 else
                              'Количество снизилось' if delta < 0 else
                              'Количество без изменений')
    runs.append(current)
    return {'version': 1, 'runs': runs}


def save_history(path, history):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)

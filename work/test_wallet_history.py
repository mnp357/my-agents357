from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime

from openpyxl import load_workbook

from liquidation_report import export
from wallet_history import append_history


def sample(run_at, changed=False):
    rows = []
    for side, liquidation in (('Long', '$94'), ('Short', '$106')):
        for rank in range(1, 9):
            wallet = f'0x{(rank if side == "Long" else rank + 100):040x}'
            qty = rank + (1 if changed and side == 'Long' and rank == 8 else 0)
            rows.append({'symbol': 'BTC', 'side': side, 'price': '$100',
                         'liquidation': liquidation, 'volume': f'${rank}M',
                         'wallet': wallet, 'hyperliquid_position_size': str(qty)})
    return {'source': 'CoinGlass', 'started_at': run_at, 'finished_at': run_at,
            'eligible_symbols': ['BTC'], 'coins': [{'symbol': 'BTC', 'error': None,
            'pages': [{'captured_at': run_at, 'rows': rows}]}]}


first = sample('2026-11-05T00:00:00Z')
second = sample('2026-11-06T00:00:00Z', changed=True)
history = append_history({'version': 1, 'runs': []}, first)
history = append_history(history, second)
assert len(history['runs']) == 2
assert all(len(run['observations']) == 14 for run in history['runs'])
longs = [row for row in history['runs'][1]['observations'] if row['side'] == 'Лонг']
shorts = [row for row in history['runs'][1]['observations'] if row['side'] == 'Шорт']
assert [row['size_usd'] for row in longs] == [n * 1000000 for n in range(8, 1, -1)]
assert [row['size_usd'] for row in shorts] == [n * 1000000 for n in range(8, 1, -1)]
assert longs[0]['quantity_change'] == '1'
assert longs[0]['change'] == 'Количество выросло'
assert longs[0]['name'] == history['runs'][0]['observations'][0]['name']
assert len(append_history(history, second)['runs']) == 2

with TemporaryDirectory() as directory:
    file = Path(directory) / 'history.xlsx'
    tracked = longs[0]['wallet']
    export(second, file, history_runs=history['runs'], activity_events=[{
        'time_ms': 1793923260000, 'name': longs[0]['name'], 'coin': 'BTC',
        'event_type': 'Закрыл позицию', 'direction': 'Close Long',
        'size_coins': '1', 'end_position': '0', 'price': '100',
        'closed_pnl': '-5', 'fee': '0.1', 'tx_hash': '0x123',
        'trade_id': '42', 'address': tracked,
    }], activity_checks=[{
        'checked_at': '2026-11-06T00:02:00Z', 'name': longs[0]['name'],
        'fills_status': 'пробел', 'positions_status': 'проверено',
        'fills_found': 1, 'note': 'Предел API', 'address': tracked,
    }])
    book = load_workbook(file, read_only=True, data_only=True)
    activity = book['История кошельков']
    assert activity.cell(4, 4).value == 'Событие'
    assert activity.cell(5, 4).value == 'Проверка кошелька'
    assert activity.cell(5, 13).value == 'Исполнения: пробел; позиции: проверено; Предел API'
    assert activity.cell(6, 4).value == 'Закрыл позицию'
    assert activity.cell(6, 10).value == -5
    assert activity.cell(6, 13).value == 'Минус по исполнению'
    assert activity.cell(6, 15).value == tracked
    sheet = book['Снимки крупных']
    assert sheet.cell(4, 5).value == 'Метка кошелька'
    assert sheet.cell(5, 1).value == datetime(2026, 11, 6, 6)
    assert sheet.cell(5, 9).value == 1
    assert sheet.cell(5, 12).value == 'Количество выросло'
    assert sheet.cell(5, 13).value == longs[0]['wallet']
    assert sheet.max_row == 32
print('Top seven per side, stable wallet label, quantity changes, and history sheet: passed')

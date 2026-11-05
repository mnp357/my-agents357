from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from wallet_activity import (connect, collect_activity, fetch_fills,
                             fill_type, report_rows)


wallet = '0x' + 'a' * 40
other = '0x' + 'b' * 40


def fill(before, size, side, time_ms, trade_id, pnl='0', liquidation=None):
    result = {'coin': 'BTC', 'startPosition': str(before), 'sz': str(size),
              'side': side, 'dir': 'Close Long' if side == 'A' else 'Open Long',
              'time': time_ms, 'tid': trade_id, 'px': '100', 'closedPnl': pnl,
              'fee': '0.1', 'feeToken': 'USDC', 'hash': '0x' + '1' * 64}
    if liquidation is not None:
        result['liquidation'] = liquidation
    return result


assert fill_type(fill(0, 2, 'B', 1, 1), wallet)[0] == 'Открыл лонг'
assert fill_type(fill(2, 1, 'B', 1, 2), wallet)[0] == 'Увеличил позицию'
assert fill_type(fill(3, 1, 'A', 1, 3), wallet)[0] == 'Сократил позицию'
assert fill_type(fill(2, 2, 'A', 1, 4), wallet)[0] == 'Закрыл позицию'
assert fill_type(fill(2, 3, 'A', 1, 5), wallet)[0] == 'Развернул позицию'
assert fill_type(fill(2, 1, 'A', 1, 6, liquidation={
    'liquidatedUser': wallet, 'method': 'market'}), wallet)[0] == 'Ликвидация частичная'
assert fill_type(fill(2, 1, 'A', 1, 7, liquidation={
    'liquidatedUser': other, 'method': 'market'}), wallet)[0] == 'Сократил позицию'


many = [fill(1, 1, 'B', n + 1, n + 1) for n in range(2001)]
def split_request(payload):
    return [row for row in many if payload['startTime'] <= row['time'] <= payload['endTime']][:2000]
rows, complete, note = fetch_fills(wallet, 0, 2001, request=split_request,
                                   pause=lambda _: None)
assert complete and note is None and len(rows) == 2001


now = datetime.now(timezone.utc)
run_at = (now - timedelta(minutes=2)).isoformat()
event_time = int((now - timedelta(minutes=1)).timestamp() * 1000)
history = {'version': 1, 'runs': [{'run_at': run_at, 'observations': [
    {'wallet': wallet, 'symbol': 'BTC', 'side': 'Лонг'}]}]}
with TemporaryDirectory() as directory:
    db = connect(Path(directory) / 'activity.sqlite3')
    calls = []
    def request(payload):
        calls.append(payload)
        return [fill(2, 1, 'A', event_time, 123, pnl='5'),
                fill(1, 1, 'A', event_time + 1, 124, pnl='-3')]
    def account_reader(address, dex):
        assert address == wallet and dex == ''
        return [{'position': {'coin': 'ETH', 'szi': '-4', 'entryPx': '2000',
                              'liquidationPx': '2200', 'unrealizedPnl': '12'}}]
    result = collect_activity(db, history, run_at, request=request,
                              account_reader=account_reader,pause=lambda _: None)
    events, checks, positions = report_rows(db)
    assert result == {'wallets': 1, 'checked': 1, 'gaps': 0}
    assert len(events) == 2 and {row['event_type'] for row in events} == {
        'Сократил позицию', 'Закрыл позицию'}
    assert {row['closed_pnl'] for row in events} == {'5', '-3'}
    assert len(checks) == 1 and checks[0]['fills_status'] == 'проверено'
    assert len(positions) == 1 and positions[0]['signed_size'] == '-4'
    assert calls[0]['type'] == 'userFillsByTime'
    db.close()

with TemporaryDirectory() as directory:
    db = connect(Path(directory) / 'activity.sqlite3')
    def incomplete_request(payload):
        raise TimeoutError('unavailable')
    result = collect_activity(db, history, run_at, request=incomplete_request,
                              account_reader=lambda address, dex: [],
                              pause=lambda _: None)
    assert result['gaps'] == 1
    assert db.execute('SELECT cursor_ms FROM wallets').fetchone()[0] == int(
        datetime.fromisoformat(run_at).timestamp() * 1000) - 1
    assert report_rows(db)[1][0]['fills_status'] == 'пробел'
    db.close()

mixed_history = {'version': 1, 'runs': [{'run_at': run_at, 'observations': [
    {'wallet': wallet, 'symbol': 'BTC', 'side': 'Лонг'},
    {'wallet': other, 'symbol': 'SOL', 'side': 'Шорт'}]}]}
with TemporaryDirectory() as directory:
    db = connect(Path(directory) / 'activity.sqlite3')
    def mixed_request(payload):
        return [fill(0, 1, 'B', event_time, 125),
                fill(0, 1, 'B', event_time + 1, 126) | {'coin': 'SOL'}]
    def mixed_account(address, dex):
        return [{'position': {'coin': coin, 'szi': '1'}}
                for coin in ('BTC', 'ETH', 'SOL')]
    result = collect_activity(db, mixed_history, run_at, request=mixed_request,
                              account_reader=mixed_account, pause=lambda _: None)
    events, checks, positions = report_rows(db)
    assert result['wallets'] == 1 and len(checks) == 1
    assert len(events) == 1 and events[0]['coin'] == 'BTC'
    assert {row['coin'] for row in positions} == {'BTC', 'ETH'}
    assert db.execute('SELECT COUNT(*) FROM tracked_wallets').fetchone()[0] == 1
    db.close()

print('Fill classification, time-window splitting, persistent events and position checks: passed')

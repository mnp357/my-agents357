import tempfile
import unittest
import json
from pathlib import Path

import trader_position_notifications as monitor


class PositionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = monitor.DB_PATH
        monitor.DB_PATH = Path(self.tmp.name) / 'positions.sqlite'

    def tearDown(self):
        monitor.DB_PATH = self.old_path
        self.tmp.cleanup()

    def item(self, size, updated, side='Buy', idx=1):
        return monitor.valid_position({
            'category': 'linear', 'symbol': 'ADAUSDT', 'positionIdx': idx,
            'side': side, 'size': size, 'entryPrice': '0.21',
            'updatedTime': updated,
        })

    def messages(self):
        with monitor.connect() as db:
            return [row[0] for row in db.execute('SELECT message FROM notifications ORDER BY id')]

    def test_open_and_full_close_notify_once(self):
        with monitor.connect() as db:
            monitor.apply_position(db, self.item('0', 100), notify=False)
            monitor.apply_position(db, self.item('10', 200))
            monitor.apply_position(db, self.item('10', 200))
            monitor.apply_position(db, self.item('4', 300))
            monitor.apply_position(db, self.item('0', 400, side=''))
            monitor.apply_position(db, self.item('0', 400, side=''))
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertIn('Открыт лонг ADAUSDT', messages[0])
        self.assertIn('Закрыт лонг ADAUSDT', messages[1])

    def test_old_position_event_cannot_reopen_closed_position(self):
        with monitor.connect() as db:
            monitor.apply_position(db, self.item('10', 200), notify=False)
            monitor.apply_position(db, self.item('0', 400, side=''))
            monitor.apply_position(db, self.item('10', 300))
            size = db.execute('SELECT size FROM positions WHERE symbol=? AND position_idx=1',
                              ('ADAUSDT',)).fetchone()[0]
        self.assertEqual(size, '0')
        self.assertEqual(len(self.messages()), 1)

    def test_close_details_use_matching_bybit_records(self):
        class Client:
            def get(self, path, params):
                assert path == '/v5/position/closed-pnl'
                return {'list': [
                    {'symbol': 'ADAUSDT', 'side': 'Buy', 'closedSize': '4',
                     'closedPnl': '12', 'avgExitPrice': '0.22', 'updatedTime': '2000'},
                    {'symbol': 'ADAUSDT', 'side': 'Buy', 'closedSize': '6',
                     'closedPnl': '-2', 'avgExitPrice': '0.23', 'updatedTime': '1900'},
                    {'symbol': 'ADAUSDT', 'side': 'Sell', 'closedSize': '10',
                     'closedPnl': '999', 'avgExitPrice': '0.99', 'updatedTime': '2000'},
                ]}
        event = {'symbol': 'ADAUSDT', 'side': 'Sell', 'size': '10',
                 'previous_ms': 1500, 'updated_ms': 2100}
        message = monitor.close_details(Client(), event)
        self.assertIn('Закрыт шорт ADAUSDT.', message)
        self.assertIn('Цена закрытия: 0.226 USDT.', message)
        self.assertIn('PnL: +10 USDT.', message)
        self.assertIn('Дата:', message)

    def test_close_queues_event_for_enrichment(self):
        with monitor.connect() as db:
            monitor.apply_position(db, self.item('10', 100, side='Sell', idx=2), notify=False)
            monitor.apply_position(db, self.item('0', 200, side='', idx=2))
            queued = db.execute('SELECT close_event FROM notifications').fetchone()[0]
        self.assertEqual(json.loads(queued)['size'], '10')


if __name__ == '__main__':
    unittest.main()

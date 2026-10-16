"""Offline checks for iPhone reminder and position transitions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import iphone_push as push


class FakeClient:
    def __init__(self, *_):
        pass


class IPhonePushTest(unittest.TestCase):
    def test_three_day_reminder_once_per_cycle_and_position_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(push, 'DATABASE', root / 'alerts.sqlite'), patch.object(push, 'Client', FakeClient):
                db = push.connect()
                db.execute('INSERT INTO devices VALUES(?,?,?)', ('a' * 64, json.dumps(['123']), 0))
                db.commit()
                positions = {}
                order = {'orderId': 'order-1', 'symbol': 'BTCUSDT', 'orderStatus': 'New',
                         'createdTime': '1000', 'side': 'Buy', 'qty': '0.1'}
                with patch.object(push, 'current_positions', side_effect=lambda _: positions.copy()), \
                     patch.object(push, 'current_orders', return_value=[order]):
                    config = {'key': 'x', 'secret': 'y', 'region': 'global'}
                    push.scan_account(db, '123', config, now_ms=1000)
                    positions[('BTCUSDT', 0)] = ('Buy', '0.1')
                    push.scan_account(db, '123', config, now_ms=1000 + push.THREE_DAYS_MS)
                    push.scan_account(db, '123', config, now_ms=1000 + push.THREE_DAYS_MS + 1000)
                    positions.clear()
                    push.scan_account(db, '123', config, now_ms=1000 + 2 * push.THREE_DAYS_MS)
                rows = db.execute('SELECT title,event_key FROM events WHERE token=? ORDER BY rowid', ('a' * 64,)).fetchall()
                self.assertEqual([row['title'] for row in rows],
                                 ['Позиция открыта', 'Ордер ожидает исполнения',
                                  'Позиция закрыта', 'Ордер ожидает исполнения'])
                self.assertEqual(len(set(row['event_key'] for row in rows)), 4)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events WHERE token=?', ('telegram',)).fetchone()[0], 4)
                db.close()


if __name__ == '__main__':
    unittest.main()

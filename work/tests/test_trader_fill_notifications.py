import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import trader_orders
import trader_fill_notifications as fills


class FillNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = trader_orders.DB_PATH
        trader_orders.DB_PATH = Path(self.tmp.name) / 'trader.sqlite'
        with trader_orders.db_connect() as db:
            db.execute('''INSERT INTO drafts (code, owner_id, created_at, expires_at,
                intent_json, params_json, position_json, existing_count, leverage,
                state, order_link_id) VALUES (?, ?, 1, 2, '{}', ?, '[]', 0, '1',
                'accepted', ?)''',
                ('short-code', 7,
                 json.dumps({'symbol': 'BTCUSDT', 'side': 'Buy', 'orderType': 'Limit'}),
                 'ma-private-id'))

    def tearDown(self):
        trader_orders.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_filled_event_uses_execution_data_and_sends_once(self):
        stamp = int(datetime(2026, 9, 26, 14, 35, tzinfo=timezone.utc).timestamp() * 1000)
        event = {'orderStatus': 'Filled', 'orderLinkId': 'ma-private-id',
                 'symbol': 'BTCUSDT', 'side': 'Buy', 'cumExecQty': '0.011',
                 'avgPrice': '84777', 'updatedTime': str(stamp)}
        self.assertTrue(fills.queue_filled(event))
        self.assertTrue(fills.queue_filled(event))
        with trader_orders.db_connect() as db:
            messages = [r[0] for r in db.execute('SELECT message FROM fill_notifications')]
        self.assertEqual(len(messages), 1)
        self.assertIn('BTCUSDT (short-code)', messages[0])
        self.assertIn('Лонг', messages[0])
        self.assertIn('84777 USDT', messages[0])
        self.assertIn('26.09.2026 20:35 (Омск)', messages[0])
        self.assertNotIn('ma-private-id', messages[0])
        with patch('telegram_bridge.state', return_value={
            'owner_id': 7, 'owner_chat_id': 10, 'token': 'test-token'}), \
                patch('telegram_bridge.send') as send:
            fills.flush()
            fills.flush()
        send.assert_called_once()

    def test_ignores_unfilled_or_unowned_order(self):
        event = {'orderStatus': 'New', 'orderLinkId': 'ma-private-id'}
        self.assertFalse(fills.queue_filled(event))
        event['orderStatus'] = 'Filled'
        event['orderLinkId'] = 'some-other-order'
        self.assertFalse(fills.queue_filled(event))


if __name__ == '__main__':
    unittest.main()

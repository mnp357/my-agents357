import unittest

from trader_status import account_status


class FakeClient:
    def __init__(self):
        self.calls = []

    def verify_trade_subaccount(self):
        return {'uid': 'owner-subaccount'}

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        symbol = params.get('symbol')
        if path == '/v5/position/list':
            if symbol:
                return {'list': [{'symbol': symbol, 'side': 'Buy', 'size': '2',
                                  'avgPrice': '0.25', 'unrealisedPnl': '0'}]}
            if not params.get('cursor'):
                return {'list': [{'symbol': 'BTCUSDT', 'side': 'Buy', 'size': '0'},
                                 {'symbol': 'ETHUSDT', 'side': 'Sell', 'size': '3',
                                  'avgPrice': '2000', 'unrealisedPnl': '-4.25'}], 'nextPageCursor': 'next'}
            return {'list': [{'symbol': 'BTCUSDT', 'side': 'Buy', 'size': '0.1',
                              'avgPrice': '80000', 'unrealisedPnl': '12.5'}]}
        if path == '/v5/order/realtime':
            if symbol:
                return {'list': [{'symbol': symbol, 'side': 'Buy', 'leavesQty': '2',
                                  'price': '0.24', 'orderType': 'Limit'}]}
            return {'list': [{'symbol': 'ETHUSDT', 'side': 'Sell', 'leavesQty': '1',
                              'price': '2100', 'orderType': 'Limit'}]}
        raise AssertionError(path)


class AccountStatusTests(unittest.TestCase):
    def test_all_symbols_with_cursor(self):
        client = FakeClient()
        reply = account_status(client, 'owner-subaccount')
        self.assertIn('Открытые позиции: 2', reply)
        self.assertIn('Активные ордера: 1', reply)
        self.assertIn('BTCUSDT · лонг', reply)
        self.assertIn('ETHUSDT · шорт', reply)
        self.assertIn('вход 80000 USDT · PnL +12.5 USDT', reply)
        self.assertIn('вход 2000 USDT · PnL -4.25 USDT', reply)
        self.assertIn('ETHUSDT · продажа · осталось 1', reply)
        self.assertEqual(client.calls[0][1]['settleCoin'], 'USDT')
        self.assertEqual(client.calls[1][1]['cursor'], 'next')
        self.assertEqual(client.calls[2][1]['openOnly'], 0)
        self.assertNotIn('orderId', reply)

    def test_one_symbol(self):
        client = FakeClient()
        reply = account_status(client, 'owner-subaccount', 'ADAUSDT')
        self.assertIn('Статус Bybit · ADAUSDT', reply)
        self.assertIn('Открытые позиции: 1', reply)
        self.assertIn('Активные ордера: 1', reply)
        self.assertIn('PnL 0 USDT', reply)
        self.assertTrue(all(params.get('symbol') == 'ADAUSDT' for _, params in client.calls))


if __name__ == '__main__':
    unittest.main()

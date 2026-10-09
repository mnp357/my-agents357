import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import trader_orders as orders
import trader_expiry as expiry
import telegram_bridge
from bybit_client import BybitUncertainError, BybitError
from trader_close import parse_close, preview_close
from trader_preview import PreviewError, parse_open, preview_open, preview_ladder


class FakeClient:
    def __init__(self):
        self.equity = '1000'
        self.position_side = ''
        self.position_size = '0'
        self.position_mode = 'hedge'
        self.uncertain = False
        self.posts = []
        self.order_status = 'New'
        self.executed = '0'
        self.cancel_requested = False

    def verify_trade_subaccount(self):
        return {'uid': 'subaccount-1'}

    def get(self, path, params=None):
        if path.endswith('account/info'):
            return {'marginMode': 'REGULAR_MARGIN'}
        if path.endswith('market/risk-limit'):
            return {'list': [{'riskLimitValue': '200000', 'maintenanceMargin': '0.0075',
                              'mmDeduction': ''}]}
        if path.endswith('wallet-balance'):
            return {'list': [{'accountType': 'UNIFIED', 'totalEquity': self.equity,
                              'totalAvailableBalance': '500'}]}
        if path.endswith('instruments-info'):
            return {'list': [{'symbol': 'ADAUSDT', 'contractType': 'LinearPerpetual',
                              'settleCoin': 'USDT', 'status': 'Trading',
                              'priceFilter': {'tickSize': '0.0001', 'minPrice': '0.0001',
                                              'maxPrice': '1000'},
                              'lotSizeFilter': {'qtyStep': '1', 'minOrderQty': '1',
                                                'maxOrderQty': '100000',
                                                'maxMktOrderQty': '100000',
                                                'minNotionalValue': '5'}}]}
        if path.endswith('market/tickers'):
            return {'list': [{'symbol': 'ADAUSDT', 'lastPrice': '0.25'}]}
        if path.endswith('market/kline'):
            return {'list': [[str(params['start']), '0.25', '0.27', '0.24', '0.26', '1', '1']]}
        if path.endswith('position/list'):
            if self.position_mode == 'one_way':
                return {'list': [{'positionIdx': 0, 'side': self.position_side,
                                  'size': self.position_size, 'leverage': '3',
                                  'markPrice': '0.25'}]}
            return {'list': [{'positionIdx': 1, 'side': self.position_side if self.position_side == 'Buy' else '',
                              'size': self.position_size if self.position_side == 'Buy' else '0',
                              'leverage': '3', 'markPrice': '0.25'},
                             {'positionIdx': 2, 'side': self.position_side if self.position_side == 'Sell' else '',
                              'size': self.position_size if self.position_side == 'Sell' else '0',
                              'leverage': '3', 'markPrice': '0.25'}]}
        if path.endswith('order/realtime'):
            status = 'Cancelled' if self.cancel_requested else self.order_status
            return {'list': [{'orderLinkId': params['orderLinkId'],
                              'orderStatus': status, 'cumExecQty': self.executed,
                              'leavesQty': '0' if status in ('Filled', 'Cancelled') else '30',
                              'symbol': 'ADAUSDT', 'side': 'Buy', 'orderType': 'Limit',
                              'qty': '40', 'orderId': 'bybit-order-1'}]}
        raise AssertionError(path)

    def post_once(self, path, payload):
        self.posts.append((path, payload))
        if self.uncertain:
            raise BybitUncertainError('timeout')
        if path.endswith('order/cancel'):
            self.cancel_requested = True
        return {'orderId': 'bybit-order-1'}


class TraderFlowTests(unittest.TestCase):
    def test_solano_short_ladder_alias(self):
        intent = parse_open('Открой Short Solano лесенкой цена 115 объем 1%')
        self.assertEqual(intent['symbol'], 'SOLUSDT')
        self.assertEqual(intent['side'], 'Sell')
        self.assertTrue(intent['ladder'])
        self.assertEqual(str(intent['price']), '115')
        self.assertEqual(str(intent['percent']), '1')

    def test_put_short_ladder_with_compact_control_price(self):
        intent = parse_open('Поставь ордер лесенкой, шорт XRP, цена 1.399, '
                            'объем 2%, контроль 1.357')
        self.assertEqual(intent['symbol'], 'XRPUSDT')
        self.assertEqual(intent['side'], 'Sell')
        self.assertTrue(intent['ladder'])
        self.assertEqual(str(intent['price']), '1.399')
        self.assertEqual(str(intent['percent']), '2')
        self.assertEqual(str(intent['control_price']), '1.357')
        self.assertEqual(intent['control_price'], intent['take_profit'])

    def test_infinitive_put_short_ladder(self):
        intent = parse_open('Поставить ордер лесенкой, шорт XRPUSDT, цена 1.417, '
                            'размер 2%, контрольная цена 1.357')
        self.assertEqual(intent['symbol'], 'XRPUSDT')
        self.assertTrue(intent['ladder'])
        self.assertEqual(str(intent['control_price']), '1.357')

    def test_spoken_eth_short_ladder_with_spaced_control_price(self):
        intent = parse_open('открой Short лесенкой у эфиру цена 2499 объем 2% '
                            'контроль 2 443')
        self.assertEqual(intent['symbol'], 'ETHUSDT')
        self.assertEqual(intent['side'], 'Sell')
        self.assertTrue(intent['ladder'])
        self.assertEqual(str(intent['price']), '2499')
        self.assertEqual(str(intent['percent']), '2')
        self.assertEqual(str(intent['control_price']), '2443')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = orders.DB_PATH
        orders.DB_PATH = Path(self.tmp.name) / 'drafts.sqlite'
        self.client = FakeClient()

    def tearDown(self):
        orders.DB_PATH = self.previous_path
        self.tmp.cleanup()

    def code_from(self, message):
        return message.split('/confirm ')[1].splitlines()[0]

    def test_open_exact_notional_and_one_time_confirmation(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала, без стопа')
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertEqual(preview['params']['qty'], '40')
        code = self.code_from(orders.create_draft(7, intent, preview))
        with orders.db_connect() as db:
            draft = db.execute('SELECT created_at, expires_at FROM drafts WHERE code=?', (code,)).fetchone()
        self.assertEqual(draft['expires_at'] - draft['created_at'], 600)
        self.assertFalse(self.client.posts)
        self.assertIn('не найден', orders.confirm(8, code, self.client, 'subaccount-1'))
        accepted = orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertIn('Bybit принял', accepted)
        self.assertNotIn('ID:', accepted)
        self.assertNotIn('Принятие заявки', accepted)
        self.assertNotIn('Через 24 часа', accepted)
        repeated = orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertIn('уже отправлена', repeated)
        self.assertNotIn('ID:', repeated)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.client.posts[0][1]['qty'], '40')
        self.assertIn('New', orders.order_status(7, code, self.client))

    def test_ladder_four_prices_last_double_notional_one_confirmation(self):
        intent = parse_open('Открой лонг ADA лесенкой, цена 0.25, размер 10 USDT')
        self.assertTrue(intent['ladder'])
        previews = preview_ladder(self.client, intent, 'subaccount-1')
        self.assertEqual([p['params']['price'] for p in previews],
                         ['0.25', '0.2475', '0.2450', '0.2425'])
        self.assertEqual([p['params']['qty'] for p in previews], ['40', '40', '40', '82'])
        code = self.code_from(orders.create_ladder_draft(7, intent, previews))
        self.assertFalse(self.client.posts)
        result = orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertIn('все 4 заявки', result)
        self.assertEqual(len(self.client.posts), 4)
        self.assertEqual([payload['price'] for _, payload in self.client.posts],
                         ['0.25', '0.2475', '0.2450', '0.2425'])
        self.assertEqual(len({payload['orderLinkId'] for _, payload in self.client.posts}), 4)
        self.assertIn('Повторной отправки не будет',
                      orders.confirm(7, code, self.client, 'subaccount-1'))
        self.assertEqual(len(self.client.posts), 4)

    def test_short_ladder_and_cancel_all_before_submission(self):
        intent = parse_open('Открой шорт ADA лесенкой, цена 0.25, размер 10 USDT')
        previews = preview_ladder(self.client, intent, 'subaccount-1')
        self.assertEqual([p['params']['price'] for p in previews],
                         ['0.25', '0.2525', '0.2550', '0.2575'])
        code = self.code_from(orders.create_ladder_draft(7, intent, previews))
        self.assertIn('Все четыре заявки не отправлены', orders.cancel_code(7, code))
        self.assertFalse(self.client.posts)
        self.assertIn('Повторной отправки не будет',
                      orders.confirm(7, code, self.client, 'subaccount-1'))

    def test_ladder_stops_after_partial_rejection_without_retry(self):
        intent = parse_open('Открой лонг ADA лесенкой, цена 0.25, размер 10 USDT')
        code = self.code_from(orders.create_ladder_draft(
            7, intent, preview_ladder(self.client, intent, 'subaccount-1')))
        original_post = self.client.post_once
        def reject_second(path, payload):
            if len(self.client.posts) == 1:
                self.client.posts.append((path, payload))
                raise BybitError('Bybit отклонил вторую заявку')
            return original_post(path, payload)
        self.client.post_once = reject_second
        result = orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertIn('подтвердил приём: 1 из 4', result)
        self.assertEqual(len(self.client.posts), 2)
        orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertEqual(len(self.client.posts), 2)

    def test_ladder_rejects_prices_collapsing_to_same_tick(self):
        intent = parse_open('Открой лонг ADA лесенкой, цена 0.001, размер 10 USDT')
        with self.assertRaisesRegex(PreviewError, 'четыре разные ступени'):
            preview_ladder(self.client, intent, 'subaccount-1')

    def test_ladder_unknown_first_result_never_sends_later_levels(self):
        intent = parse_open('Открой лонг ADA лесенкой, цена 0.25, размер 10 USDT')
        code = self.code_from(orders.create_ladder_draft(
            7, intent, preview_ladder(self.client, intent, 'subaccount-1')))
        self.client.uncertain = True
        result = orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertIn('Ответ Bybit не получен', result)
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn('Повторной отправки не будет',
                      orders.confirm(7, code, self.client, 'subaccount-1'))
        self.assertEqual(len(self.client.posts), 1)

    def test_open_fixed_usdt_amount_rounds_down_to_contract_step(self):
        intent = parse_open('Открой лонг ADA 0,23, размер позиции на 10 долларов, без стопа')
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertEqual(preview['params']['qty'], '43')
        self.assertEqual(str(preview['notional']), '9.89')
        self.assertIn('задано 10 USDT', orders.create_draft(7, intent, preview))
        with orders.db_connect() as db:
            stored = db.execute("SELECT intent_json FROM drafts WHERE owner_id=7 AND state='pending'").fetchone()[0]
        self.assertEqual(orders.decoded_intent(stored)['amount_usdt'], intent['amount_usdt'])

    def test_open_confirmation_uses_prepared_order_without_rechecking_position(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала, без стопа')
        code = self.code_from(orders.create_draft(
            7, intent, preview_open(self.client, intent, 'subaccount-1')))
        self.client.position_side = 'Buy'
        self.client.position_size = '5'
        self.assertIn('Bybit принял', orders.confirm(7, code, self.client, 'subaccount-1'))
        self.assertEqual(len(self.client.posts), 1)

    def test_open_targets_hedge_side_without_mode_audit(self):
        self.client.position_mode = 'one_way'
        self.client.position_side = 'Sell'
        self.client.position_size = '2'
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала, без стопа')
        self.assertEqual(preview_open(self.client, intent, 'subaccount-1')['params']['positionIdx'], 1)

    def test_open_keeps_hedge_index_even_if_mode_not_prechecked(self):
        self.client.position_mode = 'one_way'
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала')
        self.assertEqual(preview_open(self.client, intent, 'subaccount-1')['params']['positionIdx'], 1)

    def test_close_is_reduce_only_and_not_repeated(self):
        self.client.position_side = 'Buy'
        self.client.position_size = '40'
        intent = parse_close('Закрыть лонг ADA полностью по рынку')
        preview = preview_close(self.client, intent, 'subaccount-1')
        code = self.code_from(orders.create_draft(7, intent, preview))
        self.assertIn('на закрытие позиции', orders.confirm(7, code, self.client, 'subaccount-1'))
        payload = self.client.posts[0][1]
        self.assertIs(payload['reduceOnly'], True)
        self.assertEqual(payload['side'], 'Sell')
        self.assertEqual(payload['orderType'], 'Market')
        orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertEqual(len(self.client.posts), 1)

    def test_uncertain_result_is_never_retried(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала, без стопа')
        code = self.code_from(orders.create_draft(
            7, intent, preview_open(self.client, intent, 'subaccount-1')))
        self.client.uncertain = True
        self.assertIn('Ответ Bybit не получен', orders.confirm(7, code, self.client, 'subaccount-1'))
        orders.confirm(7, code, self.client, 'subaccount-1')
        self.assertEqual(len(self.client.posts), 1)

    def test_missing_trade_details_are_rejected(self):
        with self.assertRaises(PreviewError):
            parse_open('Открыть лонг ADA, цена 0.25')
        with self.assertRaises(PreviewError):
            parse_close('Закрыть лонг ADA')

    def test_open_without_stop_and_liquidation_message(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1%')
        self.assertNotIn('stop', intent)
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertNotIn('stopLoss', preview['params'])
        from trader_preview import format_preview
        message = format_preview(preview)
        self.assertNotIn('Стоп', message)
        self.assertIn('Оценка цены ликвидации: вне доступного диапазона', message)

    def test_large_cross_margin_preview_has_estimated_liquidation(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 200% капитала')
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertEqual(preview['liquidation_estimate'], '0.1894')
        from trader_preview import format_preview
        self.assertIn('≈ 0.1894 USDT', format_preview(preview))
        self.assertIn('Ликвидация: ≈ 0.1894 USDT', format_preview(preview))
        self.assertNotIn('кросс-маржа, текущий баланс', format_preview(preview))

    def test_confirmation_is_a_separate_button_message(self):
        sent = []
        def capture(token, chat_id, message, markup=None):
            sent.append((message, markup))
        with patch.object(telegram_bridge, 'send', side_effect=capture):
            telegram_bridge.send_trader_draft(
                'token', 7, 'Расчёт\n\nЕсли всё верно, в течение 10 минут отправьте: /confirm ABC123\nДля отмены черновика: /cancel')
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][0], 'Расчёт')
        self.assertEqual(sent[1][0], 'Подтвердить эту заявку?')
        buttons = sent[1][1]['inline_keyboard'][0]
        self.assertEqual(buttons[0]['callback_data'], 'confirm:ABC123')
        self.assertEqual(buttons[1]['callback_data'], 'cancel:ABC123')

    def test_cancel_button_only_cancels_its_own_draft(self):
        previous_path = telegram_bridge.DB_PATH
        telegram_bridge.DB_PATH = Path(self.tmp.name) / 'telegram.sqlite'
        try:
            with telegram_bridge.connect('trader') as db:
                db.execute('''INSERT INTO trader_telegram
                    (id, token, bot_username, owner_id, owner_chat_id, last_update_id)
                    VALUES (1, 'test-token', 'test-bot', 7, 8, 0)''')
            intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1%')
            preview = preview_open(self.client, intent, 'subaccount-1')
            old_code = self.code_from(orders.create_draft(7, intent, preview))
            new_code = self.code_from(orders.create_draft(7, intent, preview))
            callback = {'callback_query': {'id': 'cb-1', 'from': {'id': 7},
                        'message': {'message_id': 3, 'chat': {'id': 8, 'type': 'private'}},
                        'data': 'cancel:' + old_code}}
            with patch.object(telegram_bridge, 'api'), patch.object(telegram_bridge, 'send'):
                telegram_bridge.handle_update(callback, 'trader')
            with orders.db_connect() as db:
                state = db.execute('SELECT state FROM drafts WHERE code=?', (new_code,)).fetchone()[0]
            self.assertEqual(state, 'pending')
            self.assertFalse(self.client.posts)
        finally:
            telegram_bridge.DB_PATH = previous_path

    def submitted_order(self):
        intent = parse_open('Открыть лонг ADA, цена 0.25, размер 1% капитала')
        code = self.code_from(orders.create_draft(
            7, intent, preview_open(self.client, intent, 'subaccount-1')))
        orders.confirm(7, code, self.client, 'subaccount-1')
        with orders.db_connect() as db:
            row = db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone()
        return code, row['expiry_next_check_at']

    def test_reminder_after_five_days_without_cancelling(self):
        code, due = self.submitted_order()
        with orders.db_connect() as db:
            row = db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone()
        self.assertIsNone(row['expiry_at'])
        self.assertEqual(due - row['submitted_at'], 5 * 86400)
        self.client.order_status = 'PartiallyFilled'
        self.client.executed = '10'
        with patch.object(telegram_bridge, 'state', return_value={
            'token': 'test-token', 'owner_id': 7, 'owner_chat_id': 99}), \
             patch.object(telegram_bridge, 'send', return_value={'message_id': 42}) as send, \
             patch.object(expiry, 'notify_pending'):
            expiry.run_once(now=due-1, client=self.client, expected_uid='subaccount-1')
            send.assert_not_called()
            expiry.run_once(now=due, client=self.client, expected_uid='subaccount-1')
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn('открыта', send.call_args.args[2])
        self.assertIn('Напоминание', send.call_args.args[2])
        with orders.db_connect() as db:
            row = db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone()
        self.assertEqual(row['expiry_state'], 'scheduled')
        self.assertEqual(row['expiry_next_check_at'], due + 5 * 86400)

    def test_second_reminder_does_not_cancel(self):
        code, due = self.submitted_order()
        with patch.object(telegram_bridge, 'state', return_value={
            'token': 'test-token', 'owner_id': 7, 'owner_chat_id': 99}), \
             patch.object(telegram_bridge, 'send', return_value={'message_id': 42}) as sent, \
             patch.object(expiry, 'notify_pending'):
            expiry.run_once(now=due, client=self.client, expected_uid='subaccount-1')
            expiry.run_once(now=due+5*86400, client=self.client, expected_uid='subaccount-1')
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(len(self.client.posts), 1)

    def test_reminder_reports_age_on_fifth_day(self):
        code, due = self.submitted_order()
        with patch.object(telegram_bridge, 'state', return_value={
            'token': 'test-token', 'owner_id': 7, 'owner_chat_id': 99}), \
             patch.object(telegram_bridge, 'send', return_value={'message_id': 42}) as sent, \
             patch.object(expiry, 'notify_pending'):
            expiry.run_once(now=due, client=self.client,
                            expected_uid='subaccount-1')
        self.assertIn('Заявка открыта уже 5 дней.', sent.call_args.args[2])

    def test_control_price_is_take_profit_and_requires_owner_choice_to_cancel(self):
        import trader_control
        intent = parse_open('Открыть лонг ADA по 0.21, размер 100 USDT, '
                            'контрольная цена 0.27')
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertEqual(preview['watch_direction'], 'up')
        self.assertEqual(preview['params']['takeProfit'], '0.27')
        self.assertEqual(preview['params']['tpOrderType'], 'Market')
        from trader_preview import format_preview
        self.assertIn('Контрольная цена / тейк-профит: 0.27', format_preview(preview))
        self.assertNotIn('раз в три часа', format_preview(preview))
        self.assertNotIn('Предварительный расчёт', format_preview(preview))
        code = self.code_from(orders.create_draft(7, intent, preview))
        self.assertIn('Bybit принял', orders.confirm(7, code, self.client, 'subaccount-1'))
        with orders.db_connect() as db:
            row = dict(db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone())
        self.assertEqual(row['control_price'], '0.27')
        self.assertEqual(row['control_state'], 'active')
        with patch.object(telegram_bridge, 'state', return_value={
            'token': 'test-token', 'owner_id': 7, 'owner_chat_id': 99}), \
             patch.object(telegram_bridge, 'send', return_value={'message_id': 42}) as sent:
            trader_control.run_due(self.client, now=row['control_next_check_at'] - 1)
            self.assertEqual(len(self.client.posts), 1)
            trader_control.run_due(self.client, now=row['control_next_check_at'])
        self.assertIn('Отменить только', sent.call_args.args[2])
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn('не актуален', trader_control.choose_order(7, code, 41, True,
                                                                 client=self.client))
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn('отмену', trader_control.choose_order(7, code, 42, True,
                                                           client=self.client))
        self.assertEqual(self.client.posts[-1][0], '/v5/order/cancel')

    def test_different_control_and_take_profit_prices_are_rejected(self):
        with self.assertRaisesRegex(PreviewError, 'одной и той же'):
            parse_open('Открыть лонг ADA по 0.21, размер 100 USDT, '
                       'контрольная цена 0.27, тейк-профит 0.30')

    def test_partial_fill_prompts_about_remainder_without_closing_position(self):
        import trader_control
        intent = parse_open('Открыть лонг ADA по 0.21, размер 100 USDT, контрольная цена 0.27')
        code = self.code_from(orders.create_draft(
            7, intent, preview_open(self.client, intent, 'subaccount-1')))
        orders.confirm(7, code, self.client, 'subaccount-1')
        self.client.order_status = 'PartiallyFilled'
        self.client.executed = '10'
        with orders.db_connect() as db:
            due = db.execute('SELECT control_next_check_at FROM drafts WHERE code=?',
                             (code,)).fetchone()[0]
        with patch.object(telegram_bridge, 'state', return_value={
            'token': 'test-token', 'owner_id': 7, 'owner_chat_id': 99}), \
             patch.object(telegram_bridge, 'send', return_value={'message_id': 44}) as sent:
            trader_control.run_due(self.client, now=due)
        self.assertIn('исполнено 10', sent.call_args.args[2])
        self.assertIn('Тейк-профит', sent.call_args.args[2])
        self.assertEqual(len(self.client.posts), 1)
        self.assertIn('Оставил', trader_control.choose_order(7, code, 44, False,
                                                            client=self.client))
        self.assertEqual(len(self.client.posts), 1)

    def test_short_take_profit_and_exact_downward_level(self):
        import trader_control
        intent = parse_open('Открыть шорт ADA по 0.30, размер 100 USDT, '
                            'контрольный уровень 0.20, take profit 0.20')
        preview = preview_open(self.client, intent, 'subaccount-1')
        self.assertEqual(preview['watch_direction'], 'down')
        self.assertEqual(preview['params']['positionIdx'], 2)
        self.assertEqual(preview['params']['takeProfit'], '0.20')
        code = self.code_from(orders.create_draft(7, intent, preview))
        orders.confirm(7, code, self.client, 'subaccount-1')
        with orders.db_connect() as db:
            row = dict(db.execute('SELECT * FROM drafts WHERE code=?', (code,)).fetchone())
        # A low above the exact level must not trigger an early cancellation.
        original_get = self.client.get
        def above_level(path, params=None):
            if path.endswith('market/kline'):
                return {'list': [[str(params['start']), '0.25', '0.26', '0.2001', '0.24', '1', '1']]}
            return original_get(path, params)
        with patch.object(self.client, 'get', side_effect=above_level):
            trader_control.run_due(self.client, now=row['control_next_check_at'])
        self.assertEqual(len(self.client.posts), 1)
        with orders.db_connect() as db:
            self.assertEqual(db.execute('SELECT control_state FROM drafts WHERE code=?',
                                        (code,)).fetchone()[0], 'active')

    def test_filled_order_finishes_without_prompt(self):
        code, due = self.submitted_order()
        self.client.order_status = 'Filled'
        self.client.executed = '40'
        with patch.object(telegram_bridge, 'send') as send, patch.object(expiry, 'notify_pending'):
            expiry.run_once(now=due, client=self.client, expected_uid='subaccount-1')
        send.assert_not_called()
        self.assertEqual(len(self.client.posts), 1)
        with orders.db_connect() as db:
            self.assertEqual(db.execute('SELECT expiry_state FROM drafts WHERE code=?', (code,)).fetchone()[0], 'done')


if __name__ == '__main__':
    unittest.main()

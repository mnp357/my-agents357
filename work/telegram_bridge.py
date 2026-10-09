"""Private Telegram control channel and immediate report launcher."""
import hmac
import json
import secrets
import sqlite3
import time
import subprocess
import sys
import fcntl
import urllib.error
import urllib.request
import os
import re
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path('/var/lib/my-agents/telegram.sqlite')
job_process = None
bybit_process = None

def job_active(agent='hyperliquid'):
    filename = 'job.lock' if agent == 'hyperliquid' else 'bybit.lock'
    with open('/var/lib/my-agents/' + filename,'a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True

def job_status(agent='hyperliquid'):
    filename = 'job-status.json' if agent == 'hyperliquid' else 'bybit-status.json'
    try:
        return json.loads((Path('/var/lib/my-agents') / filename).read_text())
    except (OSError, ValueError):
        if agent == 'bybit':
            return {'state':'ready','message':'Готов к запуску после подключения ключа Bybit только для чтения.'}
        return {'state':'ready','message':'Готов к запуску. До 3 страниц каждой монеты, позиции по размеру, уровни в пределах 7%, XYZ исключён. Результат — Excel.'}

def start_job(agent='hyperliquid'):
    global job_process, bybit_process
    process = job_process if agent == 'hyperliquid' else bybit_process
    if job_active(agent) or (process is not None and process.poll() is None):
        return 'Сбор уже выполняется. /status — прогресс, /pause — остановить.'
    if agent == 'bybit':
        if not Path('/var/lib/my-agents/bybit-api.json').exists():
            return 'Сначала подключите ключ Bybit только для чтения на сайте: https://195.133.15.115/bybit'
        Path('/var/lib/my-agents/bybit-cancel').unlink(missing_ok=True)
        bybit_process = subprocess.Popen([sys.executable,'/opt/my-agents/bybit_job.py'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return 'Запускаю анализ Bybit. Соберу доступные спотовые и фьючерсные сделки, поступления и выводы. Прогресс: /status.'
    Path('/var/lib/my-agents/job-cancel').unlink(missing_ok=True)
    job_process = subprocess.Popen([sys.executable,'/opt/my-agents/agent_job.py'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return 'Запускаю сбор CoinGlass: до 3 страниц каждой монеты по размеру позиции, XYZ исключён, расстояние до ликвидации — не более 7%. Excel пришлю сюда. Проверить прогресс: /status.'


def table_for(agent):
    if agent not in ('hyperliquid', 'bybit', 'trader'):
        raise ValueError('Unknown agent')
    return {'hyperliquid': 'telegram', 'bybit': 'bybit_telegram',
            'trader': 'trader_telegram'}[agent]


@contextmanager
def connect(agent='hyperliquid'):
    table = table_for(agent)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('''CREATE TABLE IF NOT EXISTS ''' + table + ''' (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        token TEXT NOT NULL,
        bot_username TEXT NOT NULL,
        pair_code TEXT,
        owner_id INTEGER,
        owner_chat_id INTEGER,
        last_update_id INTEGER NOT NULL DEFAULT 0
    )''')
    db.commit()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def state(agent='hyperliquid'):
    table = table_for(agent)
    with connect(agent) as db:
        row = db.execute('SELECT * FROM ' + table + ' WHERE id=1').fetchone()
        return dict(row) if row else None


def api(token, method, data=None, timeout=12):
    # Telegram's Bot API requires the token in the URL. Never log this URL.
    url = 'https://api.telegram.org/bot' + token + '/' + method
    payload = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise ValueError('Telegram rejected the request')
    return result['result']


def configure(token, agent='hyperliquid'):
    table = table_for(agent)
    if not token or len(token) > 128 or ':' not in token:
        raise ValueError('Некорректный токен бота.')
    existing = state(agent)
    if existing and hmac.compare_digest(token, existing['token']):
        return existing['bot_username']
    for other_agent in ('hyperliquid', 'bybit', 'trader'):
        if other_agent != agent:
            other = state(other_agent)
            if other and hmac.compare_digest(token, other['token']):
                raise ValueError('Этот бот уже используется другим агентом. Создайте отдельного бота в BotFather.')
    try:
        bot = api(token, 'getMe')
    except (urllib.error.URLError, ValueError, TimeoutError):
        raise ValueError('Telegram не подтвердил токен. Проверьте его в BotFather.') from None
    if not bot.get('is_bot') or not bot.get('username'):
        raise ValueError('Telegram не подтвердил, что это бот.')
    try:
        webhook = api(token, 'getWebhookInfo')
    except (urllib.error.URLError, ValueError, TimeoutError):
        raise ValueError('Не удалось проверить настройки бота в Telegram.') from None
    if webhook.get('url'):
        try:
            api(token, 'deleteWebhook', {'drop_pending_updates': False})
            webhook = api(token, 'getWebhookInfo')
        except (urllib.error.URLError, ValueError, TimeoutError):
            raise ValueError('Не удалось отключить прежнее подключение бота. Повторите попытку.') from None
        if webhook.get('url'):
            raise ValueError('Прежнее подключение бота всё ещё активно. Проверьте сервис, который его использует.')
    try:
        name = {'bybit': 'Аналитик Bybit', 'hyperliquid': 'Сборщик данных Hyperliquid',
                'trader': 'Торговый агент Bybit'}[agent]
        api(token, 'setMyName', {'name': name})
        commands = ([{'command': 'status', 'description': 'Состояние торгового агента'},
                     {'command': 'balance', 'description': 'Капитал субаккаунта'},
                     {'command': 'preview', 'description': 'Подготовить лимитную заявку'},
                     {'command': 'cancel', 'description': 'Отменить черновик заявки'},
                     {'command': 'order', 'description': 'Проверить статус заявки'},
                     {'command': 'help', 'description': 'Как давать торговые команды'}]
                    if agent == 'trader' else [
                        {'command': 'activate', 'description': 'Запустить анализ и получить Excel'},
                        {'command': 'status', 'description': 'Показать прогресс'},
                        {'command': 'pause', 'description': 'Остановить выполнение'}])
        api(token, 'setMyCommands', {'commands': commands})
    except (urllib.error.URLError, ValueError, TimeoutError):
        raise ValueError('Telegram не сохранил название и команды бота. Повторите попытку.') from None
    code = secrets.token_urlsafe(18)
    with connect(agent) as db:
        db.execute('''INSERT INTO ''' + table + '''
            (id, token, bot_username, pair_code, owner_id, owner_chat_id, last_update_id)
            VALUES (1, ?, ?, ?, NULL, NULL, 0)
            ON CONFLICT(id) DO UPDATE SET token=excluded.token,
                bot_username=excluded.bot_username, pair_code=excluded.pair_code,
                owner_id=NULL, owner_chat_id=NULL, last_update_id=0''',
                   (token, bot['username'], code))
        db.commit()
    return bot['username']


def send(token, chat_id, text, reply_markup=None):
    data = {'chat_id': chat_id, 'text': text}
    if reply_markup is not None:
        data['reply_markup'] = reply_markup
    return api(token, 'sendMessage', data)


def send_trader_draft(token, chat_id, reply):
    marker = '\n\nЕсли всё верно, в течение 10 минут отправьте: /confirm '
    preview, remainder = reply.rsplit(marker, 1)
    code = remainder.splitlines()[0]
    preview_message = send(token, chat_id, preview)
    markup = {'inline_keyboard': [[
        {'text': '✅ Подтвердить', 'callback_data': 'confirm:' + code},
        {'text': '❌ Не подтверждать', 'callback_data': 'cancel:' + code},
    ]]}
    try:
        button_message = send(token, chat_id, 'Подтвердить эту заявку?', markup)
    except Exception:
        from trader_orders import cancel_code
        cancel_code_for_owner = None
        with connect('trader') as db:
            row = db.execute('SELECT owner_id FROM trader_telegram WHERE id=1').fetchone()
            if row:
                cancel_code_for_owner = row['owner_id']
        if cancel_code_for_owner is not None:
            cancel_code(cancel_code_for_owner, code)
        send(token, chat_id, 'Не удалось показать кнопки. Черновик отменён; ордер не отправлен.')
        raise
    return preview_message, button_message


def prepare_owner_open(text):
    """Prepare and deliver a trade preview to the paired owner without exposing its code."""
    from bybit_client import Client as BybitClient
    from trader_preview import parse_open, preview_open, preview_ladder
    from trader_orders import create_draft, create_ladder_draft
    with connect('trader') as db:
        bot = db.execute('SELECT token,owner_id,owner_chat_id FROM trader_telegram WHERE id=1').fetchone()
    if not bot or bot['owner_id'] is None or bot['owner_chat_id'] is None:
        raise RuntimeError('Торговый Telegram-бот не подключён к владельцу.')
    cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
    client = BybitClient(cfg['key'], cfg['secret'], cfg['region'])
    intent = parse_open(text)
    if intent.get('ladder'):
        previews = preview_ladder(client, intent, cfg['uid'])
        preview = previews[0]
        reply = create_ladder_draft(bot['owner_id'], intent, previews)
    else:
        preview = preview_open(client, intent, cfg['uid'])
        reply = create_draft(bot['owner_id'], intent, preview)
    delivered = send_trader_draft(bot['token'], bot['owner_chat_id'], reply)
    result = {'symbol': preview['params']['symbol'], 'side': preview['params']['side'],
            'price': preview['params']['price'], 'qty': preview['params']['qty'],
            'notional': str(preview['notional']),
            'telegram_message_ids': [item.get('message_id') for item in delivered]}
    if intent.get('ladder'):
        result['ladder'] = [{'price': item['params']['price'], 'qty': item['params']['qty'],
                             'notional': str(item['notional'])} for item in previews]
    return result


def handle_trader_callback(update):
    callback = update['callback_query']
    sender = callback.get('from') or {}
    message = callback.get('message') or {}
    chat = message.get('chat') or {}
    with connect('trader') as db:
        row = db.execute('SELECT * FROM trader_telegram WHERE id=1').fetchone()
    if not row:
        return
    token = row['token']
    valid_owner = (sender.get('id') == row['owner_id'] and
                   chat.get('id') == row['owner_chat_id'] and chat.get('type') == 'private')
    match = re.fullmatch(r'(confirm|cancel|keep|delete|ctrlcancel|ctrlkeep):([A-Za-z0-9_-]{12})',
                         callback.get('data') or '')
    try:
        api(token, 'answerCallbackQuery', {'callback_query_id': callback['id']})
    except Exception:
        pass
    if not valid_owner or not match:
        return
    action, code = match.groups()
    try:
        api(token, 'editMessageReplyMarkup', {'chat_id': chat['id'],
                                              'message_id': message['message_id'],
                                              'reply_markup': {'inline_keyboard': []}})
    except Exception:
        pass
    if action in ('ctrlcancel', 'ctrlkeep'):
        from trader_control import choose_order
        result = choose_order(sender['id'], code, message['message_id'], action == 'ctrlcancel')
    elif action in ('keep', 'delete'):
        from trader_expiry import choose_order
        result = choose_order(sender['id'], code, message['message_id'], action == 'keep')
    elif action == 'cancel':
        from trader_orders import cancel_code
        result = cancel_code(sender['id'], code)
    elif not Path('/var/lib/my-agents/trader-api.json').exists():
        result = 'Субаккаунт отключён. Ордер не отправлен.'
    else:
        try:
            from bybit_client import Client as BybitClient
            from trader_orders import confirm
            cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
            result = confirm(sender['id'], code,
                             BybitClient(cfg['key'], cfg['secret'], cfg['region']), cfg['uid'])
        except Exception:
            result = 'Не удалось проверить результат. Перед повтором проверьте заявку на Bybit.'
    send(token, chat['id'], result)


def voice_can_prepare_draft(text):
    """Never treat audio as a confirmation or any other Bot API command."""
    return text.lower().startswith(('открыть ', 'открой ', 'закрыть ', 'закрой '))


def handle_update(update, agent='hyperliquid'):
    if agent == 'trader' and isinstance(update.get('callback_query'), dict):
        handle_trader_callback(update)
        return
    message = update.get('message')
    if not isinstance(message, dict):
        return
    chat = message.get('chat') or {}
    sender = message.get('from') or {}
    text = (message.get('text') or '').strip()
    if chat.get('type') != 'private' or not isinstance(sender.get('id'), int):
        return
    table = table_for(agent)
    with connect(agent) as db:
        row = db.execute('SELECT * FROM ' + table + ' WHERE id=1').fetchone()
        if not row:
            return
        token = row['token']
        if row['owner_id'] is None:
            prefix = '/start '
            if not text.startswith(prefix):
                if text.split(' ', 1)[0].split('@', 1)[0].lower() == '/start':
                    send(token, chat['id'], 'Откройте страницу подключения на сайте и отправьте команду /start с кодом, который показан там сейчас.')
                return
            given = text[len(prefix):].strip()
            if not hmac.compare_digest(given, row['pair_code'] or ''):
                send(token, chat['id'], 'Код не совпал или устарел. Обновите страницу подключения на сайте и отправьте показанную там команду /start.')
                return
            db.execute('UPDATE ' + table + ' SET owner_id=?, owner_chat_id=?, pair_code=NULL WHERE id=1',
                       (sender['id'], chat['id']))
            db.commit()
            label = {'bybit': 'Аналитик Bybit', 'hyperliquid': 'Сборщик данных Hyperliquid',
                     'trader': 'Торговый агент Bybit'}[agent]
            intro = (' Пока торговля не включена. /status — состояние, /help — как давать команды.'
                     if agent == 'trader' else ' /activate — запуск, /status — прогресс, /pause — остановка.')
            send(token, chat['id'], label + ' подключён.' + intro)
            return
        if sender['id'] != row['owner_id'] or chat['id'] != row['owner_chat_id']:
            return
        if agent == 'trader' and message.get('voice'):
            from trader_voice import VoiceError, transcribe
            try:
                text = transcribe(token, message['voice'], api)
            except VoiceError as exc:
                send(token, chat['id'], str(exc))
                return
            send(token, chat['id'], 'Я расслышал: «' + text + '». Проверьте текст и расчёт ниже.')
            # Audio can prepare a draft only. In particular, spoken /confirm
            # must never be turned into an order submission.
            if not voice_can_prepare_draft(text):
                send(token, chat['id'], 'Голосом можно подготовить открытие или закрытие позиции. Для отправки ордера нажмите отдельную кнопку подтверждения.')
                return
        command = text.split(' ', 1)[0].split('@', 1)[0].lower()
        if agent == 'trader':
            connected = Path('/var/lib/my-agents/trader-api.json').exists()
            if command == '/status' or text.lower().startswith(('проверь статус', 'проверка статуса')):
                if connected:
                    try:
                        from bybit_client import Client as BybitClient
                        from trader_status import account_status
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        client = BybitClient(cfg['key'], cfg['secret'], cfg['region'])
                        symbol = None
                        if command == '/status' and ' ' in text:
                            symbol = text.split(' ', 1)[1].strip().upper()
                            if not symbol.endswith('USDT'):
                                symbol += 'USDT'
                            if not re.fullmatch(r'[A-Z0-9]{1,20}USDT', symbol):
                                raise ValueError('Укажите монету после /status, например /status ADA.')
                        reply = account_status(client, cfg['uid'], symbol)
                    except Exception as exc:
                        reply = 'Проверка статуса не завершена: ' + str(exc)[:160]
                else:
                    reply = 'Торговый агент подключён к Telegram. Субаккаунт Bybit пока не подключён; отправка ордеров выключена.'
            elif command in ('/help', '/start'):
                reply = ('Для расчёта напишите или продиктуйте: «Открыть лонг ADA, цена 0.25, размер 1% капитала» '
                         'или «Открыть лонг ADA, цена 0.23, размер позиции на 10 USDT». '
                         'Добавьте «лесенкой»: четыре цены — исходная, затем минус 1%, 2%, 3% для лонга '
                         '(для шорта плюс 1%, 2%, 3%). Последняя заявка двойного объёма; '
                         'всего около 5 размеров одной заявки. '
                         'Размер — полная стоимость позиции, без умножения на плечо. '
                         'Для закрытия: «Закрыть лонг ADA полностью по рынку». '
                         'Я покажу точную заявку; на Bybit она отправится только после нажатия кнопки «Подтвердить». '
                         'Можно добавить «контрольная цена 0.27»: это одновременно тейк-профит для исполненной части. '
                         'Контрольную цену проверяю по истории Bybit раз в три часа; если заявка ещё открыта, спрошу кнопками, отменить ли её остаток. '
                         'О всё ещё открытой заявке напоминаю раз в 5 дней. '
                         '«Не подтверждать» отменяет черновик, не биржевой ордер. '
                         '«Проверь ордера» или /orders покажет открытые заявки отдельно. '
                         '/status покажет все открытые позиции и ордера USDT-фьючерсов; '
                         '/status ADA — только ADAUSDT.')
            elif command == '/cancel':
                from trader_orders import cancel
                reply = cancel(sender['id'])
            elif command == '/order':
                code = text.split(' ', 1)[1].strip() if ' ' in text else ''
                if not code:
                    reply = 'Укажите код из предварительного расчёта: /order КОД'
                elif not connected:
                    reply = 'Субаккаунт отключён. Проверьте статус непосредственно на Bybit.'
                else:
                    try:
                        from bybit_client import Client as BybitClient
                        from trader_orders import order_status
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        reply = order_status(sender['id'], code,
                                             BybitClient(cfg['key'], cfg['secret'], cfg['region']))
                    except Exception:
                        reply = 'Не удалось проверить статус. Проверьте ордер непосредственно на Bybit.'
            elif command == '/confirm':
                reply = 'Подтверждение кодом отключено. Подготовьте новую заявку и нажмите кнопку «Подтвердить» под её расчётом.'
            elif command == '/balance':
                if not connected:
                    reply = 'Сначала подключите торговый субаккаунт на сайте: https://195.133.15.115/bybit/trader'
                else:
                    try:
                        from bybit_client import Client as BybitClient, BybitError
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        wallet = BybitClient(cfg['key'], cfg['secret'], cfg['region']).get(
                            '/v5/account/wallet-balance', {'accountType': 'UNIFIED'}).get('list') or []
                        if len(wallet) != 1:
                            reply = 'Bybit не вернул баланс субаккаунта.'
                        else:
                            reply = 'Капитал субаккаунта: ' + str(wallet[0].get('totalEquity', '—')) + ' USD.'
                    except Exception:
                        reply = 'Не удалось прочитать баланс Bybit. Проверьте подключение на сайте.'
            elif command == '/orders' or text.lower().startswith(('проверь ордера', 'проверить ордера')):
                if not connected:
                    reply = 'Сначала подключите торговый субаккаунт на сайте: https://195.133.15.115/bybit/trader'
                else:
                    try:
                        from bybit_client import Client as BybitClient, pages
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        client = BybitClient(cfg['key'], cfg['secret'], cfg['region'])
                        match = re.search(r'\b([A-Za-z0-9]{2,20})(?:USDT)?\s*$', text)
                        symbol = None
                        if match and match.group(1).lower() not in ('orders', 'ордера'):
                            symbol = match.group(1).upper()
                            if not symbol.endswith('USDT'):
                                symbol += 'USDT'
                        params = {'category': 'linear', 'openOnly': 0, 'limit': 50}
                        params.update({'symbol': symbol} if symbol else {'settleCoin': 'USDT'})
                        rows = list(pages(client, '/v5/order/realtime', params, 'list'))
                        if rows:
                            lines = [f'{r.get("symbol", "—")}: {"покупка" if r.get("side") == "Buy" else "продажа"} '
                                     f'{r.get("leavesQty") or r.get("qty") or "—"} по '
                                     f'{r.get("price") or "рынку"} USDT, {r.get("orderStatus") or "—"}'
                                     for r in rows[:30]]
                            reply = 'Открытые заявки (' + str(len(rows)) + '):\n' + '\n'.join(lines)
                            if len(rows) > 30:
                                reply += '\nПоказаны первые 30.'
                        else:
                            reply = 'Открытых заявок USDT-фьючерсов нет.'
                    except Exception:
                        reply = 'Не удалось получить открытые заявки Bybit. Попробуйте позже.'
            elif command == '/close' or text.lower().startswith(('закрыть ', 'закрой ')):
                if not connected:
                    reply = 'Сначала подключите торговый субаккаунт на сайте: https://195.133.15.115/bybit/trader'
                else:
                    try:
                        from bybit_client import Client as BybitClient, BybitError
                        from trader_close import parse_close, preview_close
                        from trader_preview import PreviewError
                        from trader_orders import create_draft
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        client = BybitClient(cfg['key'], cfg['secret'], cfg['region'])
                        intent = parse_close(text)
                        reply = create_draft(sender['id'], intent, preview_close(client, intent, cfg['uid']))
                        send_trader_draft(token, chat['id'], reply)
                        return
                    except (PreviewError, BybitError) as exc:
                        reply = str(exc)
                    except Exception:
                        reply = 'Не удалось подготовить закрытие. Ордер не отправлен.'
            elif command in ('/preview', '/open') or text.lower().startswith(('открыть ', 'открой ')):
                if not connected:
                    reply = 'Сначала подключите торговый субаккаунт на сайте: https://195.133.15.115/bybit/trader'
                else:
                    try:
                        from bybit_client import Client as BybitClient, BybitError
                        from trader_preview import parse_open, preview_open, preview_ladder, PreviewError
                        from trader_orders import create_draft, create_ladder_draft
                        cfg = json.loads(Path('/var/lib/my-agents/trader-api.json').read_text())
                        client = BybitClient(cfg['key'], cfg['secret'], cfg['region'])
                        intent = parse_open(text)
                        if intent.get('ladder'):
                            reply = create_ladder_draft(sender['id'], intent,
                                preview_ladder(client, intent, cfg['uid']))
                        else:
                            preview = preview_open(client, intent, cfg['uid'])
                            reply = create_draft(sender['id'], intent, preview)
                        send_trader_draft(token, chat['id'], reply)
                        return
                    except (PreviewError, BybitError) as exc:
                        reply = str(exc)
                    except Exception:
                        reply = 'Не удалось рассчитать заявку. Проверьте подключение субаккаунта.'
            elif text:
                reply = 'Я понимаю открытие лимитного USDT-фьючерса и полное закрытие по рынку. Напишите /help для примеров. Ордер не отправлен.'
            else:
                reply = 'Отправьте торговое поручение текстом или /status.'
            send(token, chat['id'], reply)
            return
        if command == '/activate':
            reply = start_job(agent)
        elif command == '/pause':
            process = job_process if agent == 'hyperliquid' else bybit_process
            if job_active(agent) or (process is not None and process.poll() is None):
                Path('/var/lib/my-agents/' + ('job-cancel' if agent == 'hyperliquid' else 'bybit-cancel')).touch()
                reply = 'Останавливаю сбор после текущей операции. Уже собранные данные пришлю в Excel.'
            else:
                reply = 'Активного сбора сейчас нет.'
        elif command == '/status':
            reply = job_status(agent)['message']
        else:
            reply = 'Команды: /activate — запуск; /status — прогресс; /pause — остановка.'
        send(token, chat['id'], reply)


def poll_forever(agent='hyperliquid'):
    table = table_for(agent)
    while True:
        current = state(agent)
        if not current:
            time.sleep(5)
            continue
        token = current['token']
        try:
            updates = api(token, 'getUpdates', {
                'offset': current['last_update_id'] + 1,
                'timeout': 25,
                'allowed_updates': ['message', 'callback_query'] if agent == 'trader' else ['message'],
            }, timeout=35)
            for update in updates:
                handle_update(update, agent)
                with connect(agent) as db:
                    db.execute('UPDATE ' + table + ' SET last_update_id=? WHERE id=1 AND last_update_id < ?',
                               (update['update_id'], update['update_id']))
                    db.commit()
        except (urllib.error.URLError, ValueError, TimeoutError, OSError, sqlite3.Error):
            # Avoid printing exceptions: Telegram errors can contain the token URL.
            time.sleep(5)


if __name__ == '__main__':
    poll_forever(os.environ.get('MY_AGENTS_BOT', 'hyperliquid'))

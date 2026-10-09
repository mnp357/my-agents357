import hashlib
import hmac
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, redirect, render_template_string, request, send_from_directory, session, jsonify
from telegram_bridge import configure as configure_telegram, state as telegram_state, job_status
from bybit_client import Client as BybitClient, BybitError

AUTH_FILE = Path('/var/lib/my-agents/auth.json')
SETTINGS = json.loads(AUTH_FILE.read_text())
PUBLIC = Path('/var/www/my-agents')
app = Flask(__name__)
app.secret_key = SETTINGS['session_secret']
app.permanent_session_lifetime = timedelta(days=14)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Strict')


@app.after_request
def prevent_setup_cache(response):
    if request.path in {'/telegram', '/telegram/bybit', '/telegram/trader',
                        '/bybit', '/bybit/trader', '/account'}:
        response.headers['Cache-Control'] = 'no-store'
    return response

LOGIN = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Вход — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card"><div class="brand"><span class="icon">✳</span>Мои агенты</div><h1>Вход в личное пространство</h1><p>Введите логин и пароль для доступа к вашим агентам.</p>{% if error %}<div class="error" role="alert">Неверный логин или пароль. Попробуйте ещё раз.</div>{% endif %}<form method="post" action="/login"><label for="username">Логин</label><input id="username" name="username" autocomplete="username" required autofocus><label for="password">Пароль</label><input id="password" type="password" name="password" autocomplete="current-password" required><button type="submit">Войти</button></form><div class="foot">Доступ только для владельца</div></main></body></html>'''

ACCOUNT = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Сменить пароль — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Сменить пароль</h1><p>Выберите свой пароль. Он должен содержать не менее 12 символов; можно использовать запоминающуюся фразу.</p>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}<form method="post" action="/account"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="current">Текущий пароль</label><input id="current" type="password" name="current" autocomplete="current-password" required autofocus><label for="new">Новый пароль</label><input id="new" type="password" name="new" autocomplete="new-password" minlength="12" required><label for="confirm">Повторите новый пароль</label><input id="confirm" type="password" name="confirm" autocomplete="new-password" minlength="12" required><button type="submit">Сохранить новый пароль</button></form><div class="foot"><a href="/">← Вернуться к агентам</a></div></main></body></html>'''

TELEGRAM = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Telegram — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card wide"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Подключить Telegram</h1>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}{% if bot %}<p>Бот <strong>@{{ bot.username }}</strong> {% if bot.paired %}подключён к вашему Telegram.{% else %}создан, но ещё не связан с вашим аккаунтом.{% endif %}</p>{% if not bot.paired %}<div class="step"><strong>Последний шаг</strong><p>Откройте бота и отправьте ему эту команду:</p><div class="command">/start {{ bot.code }}</div><a class="action" href="https://t.me/{{ bot.username }}?start={{ bot.code }}" target="_blank" rel="noopener noreferrer">Открыть бота в Telegram ↗</a></div>{% else %}<div class="step"><strong>Бот готов принимать команды</strong><p><code>/agents</code> — выбрать агента<br><code>/agent hyperliquid</code> или <code>/agent bybit</code> — переключить задание<br><code>/activate</code> — запустить выбранного агента<br><code>/status</code> — прогресс<br><code>/pause</code> — остановить текущий сбор</p><p>Этот бот выполняет только сбор данных Hyperliquid.</p></div>{% endif %}<details><summary>Подключить другого бота</summary><form method="post" action="/telegram"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="token">Новый токен из BotFather</label><input id="token" type="password" name="token" autocomplete="off" required><button type="submit">Проверить и подключить</button></form></details>{% else %}<p>Первый шаг — создайте своего бота через <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a>: отправьте ему <code>/newbot</code> и получите токен. Вставьте токен только здесь, на вашем сайте; в чат Codex его присылать не нужно.</p><form method="post" action="/telegram"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="token">Токен бота</label><input id="token" type="password" name="token" autocomplete="off" required><button type="submit">Проверить и подключить</button></form><p class="small">После проверки сайт покажет одноразовую команду, чтобы бот принимал задания только от вас.</p>{% endif %}<div class="foot"><a href="/">← Вернуться к агентам</a></div></main></body></html>'''

BYBIT = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Bybit — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card wide"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Подключить Bybit</h1><p>Агент прочитает доступную историю спотовых и фьючерсных сделок, поступлений, выводов и переводов и подготовит Excel.</p>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}{% if saved %}<div class="step"><strong>Подключено</strong><p>Ключ проверен: доступ только для чтения. Откройте отдельного бота «Аналитик Bybit» и отправьте ему <code>/activate</code>.</p></div>{% else %}<div class="step"><strong>Безопасное подключение</strong><p>В Bybit создайте системный API-ключ с правами только на чтение. Права на торговлю и вывод средств не нужны. Если задан список разрешённых IP, укажите IP вашего сервера: <code>195.133.15.115</code>.</p></div>{% endif %}<form method="post" action="/bybit"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="region">Регион аккаунта</label><select id="region" name="region"><option value="global">Обычный Bybit</option><option value="kazakhstan">Казахстан</option><option value="turkey">Турция</option><option value="georgia">Грузия</option><option value="uae">ОАЭ</option><option value="indonesia">Индонезия</option></select><label for="key">API Key</label><input id="key" type="password" name="key" autocomplete="off" required><label for="secret">API Secret</label><input id="secret" type="password" name="secret" autocomplete="off" required><button type="submit">Проверить и подключить</button></form><p class="small">Ключ проверяется через Bybit и сохраняется только на вашем сервере. В чат его присылать не нужно. Доступный период ограничен правилами API Bybit; недоступные данные будут отмечены в отчёте.</p>{% if saved %}<form method="post" action="/bybit/disconnect"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Отключить ключ</button></form>{% endif %}<div class="foot"><a href="/#agent/bybit">← Вернуться к агенту</a></div></main></body></html>'''

BYBIT_TELEGRAM = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Telegram для Bybit — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card wide"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Бот «Аналитик Bybit»</h1><p>Это отдельный чат. Его команды запускают только анализ Bybit.</p>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}{% if bot %}<div class="step"><strong>@{{ bot.username }}</strong>{% if bot.paired %}<p>Бот подключён. Команды: <code>/activate</code> — анализ, <code>/status</code> — прогресс, <code>/pause</code> — остановка.</p>{% else %}<p>Откройте его в Telegram и отправьте команду:</p><div class="command">/start {{ bot.code }}</div><a href="https://t.me/{{ bot.username }}?start={{ bot.code }}" target="_blank" rel="noopener noreferrer">Открыть бота в Telegram ↗</a>{% endif %}</div>{% else %}<div class="step"><strong>Создайте отдельного бота</strong><p>Откройте @BotFather, отправьте <code>/newbot</code>. Название: <strong>Аналитик Bybit</strong>. Имя пользователя выберите свободное. Полученный токен вставьте ниже.</p><a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">Открыть BotFather ↗</a></div>{% endif %}<form method="post" action="/telegram/bybit"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="token">Токен нового бота</label><input id="token" type="password" name="token" autocomplete="off" required><button type="submit">Проверить и подключить</button></form><p class="small">Токен вводится только на вашем сайте. Бот Hyperliquid продолжит работать отдельно.</p><div class="foot"><a href="/#agent/bybit">← Вернуться к агенту</a></div></main></body></html>'''

TRADER_KEY_FILE = Path('/var/lib/my-agents/trader-api.json')
TRADER = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Торговый субаккаунт Bybit — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card wide"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Торговый субаккаунт Bybit</h1><p>Отдельный доступ для третьего агента. Подключение ключа не размещает ордера. Каждую лимитную заявку агент покажет в Telegram перед отдельным подтверждением.</p>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}{% if uid %}<div class="step"><strong>Субаккаунт подключён</strong><p>UID: <code>{{ uid }}</code>. Ключ проверен. Подключение само по себе не отправляет ордера.</p></div>{% else %}<div class="step"><strong>Отдельный ключ субаккаунта</strong><p>Создайте ключ именно торгового субаккаунта Bybit: права ContractTrade → Order, без прав на вывод и переводы. Если Bybit позволяет ограничить IP, укажите адрес сервера <code>195.133.15.115</code>. Ключ аналитика Bybit сюда не подходит.</p></div>{% endif %}<form method="post" action="/bybit/trader"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="region">Регион аккаунта</label><select id="region" name="region"><option value="global">Обычный Bybit</option><option value="kazakhstan">Казахстан</option><option value="turkey">Турция</option><option value="georgia">Грузия</option><option value="uae">ОАЭ</option><option value="indonesia">Индонезия</option></select><label for="key">API Key субаккаунта</label><input id="key" type="password" name="key" autocomplete="off" required><label for="secret">API Secret субаккаунта</label><input id="secret" type="password" name="secret" autocomplete="off" required><button type="submit">Проверить субаккаунт</button></form><p class="small">Введите ключ только здесь, не в чат. После проверки ключ хранится на этом сервере для чтения данных субаккаунта и торговых команд; его можно отключить ниже.</p>{% if uid %}<form method="post" action="/bybit/trader/disconnect"><input type="hidden" name="csrf" value="{{ csrf }}"><button type="submit">Отключить торговый ключ</button></form>{% endif %}<div class="foot"><a href="/#agent/bybit-trader">← Вернуться к агенту</a></div></main></body></html>'''

TRADER_TELEGRAM = '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Telegram торгового агента — Мои агенты</title><link rel="stylesheet" href="/login.css"></head><body><main class="card wide"><a class="brand" href="/"><span class="icon">✳</span>Мои агенты</a><h1>Бот «Торговый агент Bybit»</h1><p>У третьего агента будет отдельный чат Telegram, чтобы его команды не смешивались со сбором данных и анализом.</p>{% if error %}<div class="error" role="alert">{{ error }}</div>{% endif %}{% if bot %}<div class="step"><strong>@{{ bot.username }}</strong>{% if bot.paired %}<p>Бот связан с вашим Telegram. <code>/status</code> — состояние, <code>/help</code> — подсказка. Отправка ордера требует отдельного подтверждения в Telegram.</p><a href="https://t.me/{{ bot.username }}" target="_blank" rel="noopener noreferrer">Открыть чат ↗</a>{% else %}<p>Откройте бота и отправьте команду:</p><div class="command">/start {{ bot.code }}</div><a href="https://t.me/{{ bot.username }}?start={{ bot.code }}" target="_blank" rel="noopener noreferrer">Открыть бота ↗</a>{% endif %}</div>{% else %}<div class="step"><strong>Создайте третьего бота</strong><p>В @BotFather отправьте <code>/newbot</code>. Название: <strong>Торговый агент Bybit</strong>. Токен вставьте ниже, не в чат Codex.</p><a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">Открыть BotFather ↗</a></div>{% endif %}<form method="post" action="/telegram/trader"><input type="hidden" name="csrf" value="{{ csrf }}"><label for="token">Токен бота</label><input id="token" type="password" name="token" autocomplete="off" required><button type="submit">Проверить и подключить</button></form><p class="small">Бот готовит лимитное открытие или полное закрытие позиции по текстовой команде. Он отправит её на Bybit только после вашей отдельной команды /confirm с одноразовым кодом; черновик действует 3 минуты.</p><div class="foot"><a href="/#agent/bybit-trader">← Вернуться к агенту</a></div></main></body></html>'''


def settings():
    return json.loads(AUTH_FILE.read_text())


def password_matches(password, cfg):
    if len(password) > 1024:
        return False
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(cfg['salt']), n=2**14, r=8, p=1)
    return hmac.compare_digest(actual.hex(), cfg['password_hash'])


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


def valid_csrf():
    return hmac.compare_digest(request.form.get('csrf', ''), session.get('csrf', '!'))


def signed_in():
    cfg = settings()
    return session.get('user') == cfg['username'] and session.get('version') == cfg.get('version', 1)




@app.get('/login')
def login_page():
    if signed_in():
        return redirect('/')
    return render_template_string(LOGIN, error=False)


@app.get('/login.css')
def login_style():
    return send_from_directory(PUBLIC, 'login.css')


@app.post('/login')
def login_submit():
    cfg = settings()
    username = request.form.get('username', '')
    password = request.form.get('password', '')
    valid = hmac.compare_digest(username, cfg['username']) and password_matches(password, cfg)
    if not valid:
        return render_template_string(LOGIN, error=True)
    session.clear()
    session['user'] = cfg['username']
    session['version'] = cfg.get('version', 1)
    session.permanent = True
    return redirect('/')


@app.get('/account')
def account_page():
    if not signed_in():
        return redirect('/login')
    return render_template_string(ACCOUNT, error=None, csrf=csrf_token())


@app.post('/account')
def account_submit():
    if not signed_in():
        return redirect('/login')
    if not valid_csrf():
        return render_template_string(ACCOUNT, error='Обновите страницу и повторите попытку.', csrf=csrf_token())
    cfg = settings()
    current = request.form.get('current', '')
    new = request.form.get('new', '')
    confirm = request.form.get('confirm', '')
    if not password_matches(current, cfg):
        return render_template_string(ACCOUNT, error='Текущий пароль неверный.', csrf=csrf_token())
    if new != confirm:
        return render_template_string(ACCOUNT, error='Новые пароли не совпадают.', csrf=csrf_token())
    if len(new) < 12 or len(new) > 128:
        return render_template_string(ACCOUNT, error='Новый пароль должен содержать от 12 до 128 символов.', csrf=csrf_token())
    salt = secrets.token_bytes(16)
    cfg['salt'] = salt.hex()
    cfg['password_hash'] = hashlib.scrypt(new.encode(), salt=salt, n=2**14, r=8, p=1).hex()
    cfg['version'] = cfg.get('version', 1) + 1
    temporary = AUTH_FILE.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(cfg, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, AUTH_FILE)
    session.clear()
    return redirect('/login')


@app.get('/telegram')
def telegram_page():
    if not signed_in():
        return redirect('/login')
    raw = telegram_state()
    bot = None if raw is None else {
        'username': raw['bot_username'],
        'paired': raw['owner_id'] is not None,
        'code': raw['pair_code'],
    }
    return render_template_string(TELEGRAM, bot=bot, error=None, csrf=csrf_token())


@app.post('/telegram')
def telegram_submit():
    if not signed_in():
        return redirect('/login')
    if not valid_csrf():
        return render_template_string(TELEGRAM, bot=None, error='Обновите страницу и повторите попытку.', csrf=csrf_token())
    try:
        configure_telegram(request.form.get('token', '').strip())
    except ValueError as exc:
        return render_template_string(TELEGRAM, bot=None, error=str(exc), csrf=csrf_token())
    return redirect('/telegram')


@app.get('/telegram/bybit')
def bybit_telegram_page():
    if not signed_in(): return redirect('/login')
    raw = telegram_state('bybit')
    bot = None if raw is None else {'username': raw['bot_username'],
                                   'paired': raw['owner_id'] is not None, 'code': raw['pair_code']}
    return render_template_string(BYBIT_TELEGRAM, bot=bot, error=None, csrf=csrf_token())


@app.post('/telegram/bybit')
def bybit_telegram_submit():
    if not signed_in(): return redirect('/login')
    if not valid_csrf():
        return render_template_string(BYBIT_TELEGRAM, bot=None, error='Обновите страницу и повторите попытку.', csrf=csrf_token())
    try:
        configure_telegram(request.form.get('token', '').strip(), 'bybit')
    except ValueError as exc:
        return render_template_string(BYBIT_TELEGRAM, bot=None, error=str(exc), csrf=csrf_token())
    return redirect('/telegram/bybit')


@app.get('/telegram/trader')
def trader_telegram_page():
    if not signed_in(): return redirect('/login')
    raw = telegram_state('trader')
    bot = None if raw is None else {'username': raw['bot_username'],
                                   'paired': raw['owner_id'] is not None, 'code': raw['pair_code']}
    return render_template_string(TRADER_TELEGRAM, bot=bot, error=None, csrf=csrf_token())


@app.post('/telegram/trader')
def trader_telegram_submit():
    if not signed_in(): return redirect('/login')
    if not valid_csrf():
        return render_template_string(TRADER_TELEGRAM, bot=None, error='Обновите страницу и повторите попытку.', csrf=csrf_token())
    try:
        configure_telegram(request.form.get('token', '').strip(), 'trader')
    except ValueError as exc:
        return render_template_string(TRADER_TELEGRAM, bot=None, error=str(exc), csrf=csrf_token())
    return redirect('/telegram/trader')


@app.get('/bybit')
def bybit_page():
    if not signed_in(): return redirect('/login')
    return render_template_string(BYBIT, error=None, saved=Path('/var/lib/my-agents/bybit-api.json').exists(), csrf=csrf_token())


@app.post('/bybit')
def bybit_submit():
    if not signed_in(): return redirect('/login')
    if not valid_csrf():
        return render_template_string(BYBIT, error='Обновите страницу и повторите попытку.', saved=False, csrf=csrf_token())
    key = request.form.get('key', '').strip()
    secret = request.form.get('secret', '').strip()
    region = request.form.get('region', '')
    if not key or not secret or len(key) > 256 or len(secret) > 512:
        error = 'Введите API Key и API Secret.'
    else:
        try:
            client = BybitClient(key, secret, region)
            client.verify_read_only()
            path = Path('/var/lib/my-agents/bybit-api.json')
            temp = path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({'key': key, 'secret': secret, 'region': region}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
            return redirect('/bybit')
        except BybitError as exc:
            error = str(exc)
    return render_template_string(BYBIT, error=error, saved=Path('/var/lib/my-agents/bybit-api.json').exists(), csrf=csrf_token())


@app.post('/bybit/disconnect')
def bybit_disconnect():
    if not signed_in(): return redirect('/login')
    if not valid_csrf(): return '', 400
    if job_status('bybit').get('state') == 'running':
        return render_template_string(BYBIT, error='Сначала остановите текущий сбор командой /pause.', saved=True, csrf=csrf_token())
    Path('/var/lib/my-agents/bybit-api.json').unlink(missing_ok=True)
    return redirect('/bybit')


@app.get('/bybit/trader')
def trader_page():
    if not signed_in(): return redirect('/login')
    saved = json.loads(TRADER_KEY_FILE.read_text()) if TRADER_KEY_FILE.exists() else {}
    return render_template_string(TRADER, error=None, uid=saved.get('uid'), csrf=csrf_token())


@app.post('/bybit/trader')
def trader_submit():
    if not signed_in(): return redirect('/login')
    if not valid_csrf():
        return render_template_string(TRADER, error='Обновите страницу и повторите попытку.', uid=None, csrf=csrf_token())
    key = request.form.get('key', '').strip()
    secret = request.form.get('secret', '').strip()
    region = request.form.get('region', '')
    if not key or not secret or len(key) > 256 or len(secret) > 512:
        error = 'Введите API Key и API Secret субаккаунта.'
    else:
        try:
            identity = BybitClient(key, secret, region).verify_trade_subaccount()
            temporary = TRADER_KEY_FILE.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump({'key': key, 'secret': secret, 'region': region,
                           'uid': identity['uid'], 'parent_uid': identity['parent_uid']}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, TRADER_KEY_FILE)
            return redirect('/bybit/trader')
        except BybitError as exc:
            error = str(exc)
    saved = json.loads(TRADER_KEY_FILE.read_text()) if TRADER_KEY_FILE.exists() else {}
    return render_template_string(TRADER, error=error, uid=saved.get('uid'), csrf=csrf_token())


@app.post('/bybit/trader/disconnect')
def trader_disconnect():
    if not signed_in(): return redirect('/login')
    if not valid_csrf(): return '', 400
    TRADER_KEY_FILE.unlink(missing_ok=True)
    return redirect('/bybit/trader')


@app.post('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.get('/')
def index():
    if not signed_in():
        return redirect('/login')
    return send_from_directory(PUBLIC, 'index.html')


@app.get('/api/agent/status')
def agent_status():
    if not signed_in(): return '', 401
    if request.args.get('agent') == 'trader':
        bot = telegram_state('trader')
        paired = bool(bot and bot['owner_id'])
        key_connected = TRADER_KEY_FILE.exists()
        return jsonify({'state': 'ready' if paired and key_connected else 'setup', 'bot_paired': paired,
                        'key_connected': key_connected,
                        'message': ('Telegram подключён. ' if paired else 'Telegram не подключён. ') +
                                   ('Субаккаунт подключён. ' if key_connected else 'Субаккаунт не подключён. ') +
                                   'Открытие лимитной заявки и полное закрытие позиции требуют отдельного подтверждения в Telegram.'})
    agent = 'bybit' if request.args.get('agent') == 'bybit' else 'hyperliquid'
    return jsonify(job_status(agent))


@app.get('/reports/<filename>')
def report_file(filename):
    if not signed_in(): return redirect('/login')
    if not filename.endswith('.xlsx'): return '', 404
    return send_from_directory('/var/lib/my-agents/reports',filename,as_attachment=True)


@app.get('/<path:filename>')
def asset(filename):
    if not signed_in():
        return redirect('/login')
    if filename not in {'style.css', 'app.js'}:
        return '', 404
    return send_from_directory(PUBLIC, filename)

"""Bybit V5 access. Credentials never enter logs or reports."""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

HOSTS = {
    'global': 'https://api.bybit.com',
    'testnet': 'https://api-testnet.bybit.com',
    'kazakhstan': 'https://api.bybit.kz',
    'turkey': 'https://api.bybit.tr',
    'georgia': 'https://api.bybitgeorgia.ge',
    'uae': 'https://api.bybit.ae',
    'indonesia': 'https://api.bybit.id',
}


class BybitError(Exception):
    pass


class BybitUncertainError(BybitError):
    """A write request may have reached Bybit but its result was not received."""


class Client:
    def __init__(self, key, secret, region='global'):
        if region not in HOSTS:
            raise BybitError('Неизвестный регион Bybit.')
        self.key, self.secret, self.base = key, secret, HOSTS[region]

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        timestamp = str(int(time.time() * 1000))
        window = '10000'
        payload = timestamp + self.key + window + query
        signature = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': self.key, 'X-BAPI-TIMESTAMP': timestamp,
                   'X-BAPI-RECV-WINDOW': window, 'X-BAPI-SIGN': signature}
        request = urllib.request.Request(self.base + path + ('?' + query if query else ''), headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = json.load(response)
                code = body.get('retCode')
                if code == 0:
                    return body.get('result') or {}
                if code in (10006, 10016) and attempt < 3:
                    time.sleep(2 ** attempt + 1)
                    continue
                raise BybitError('Bybit вернул ошибку ' + str(code) + ': ' + str(body.get('retMsg', ''))[:160])
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt + 1)
                    continue
                raise BybitError('Bybit недоступен: HTTP ' + str(exc.code)) from None
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(2 ** attempt + 1)
                    continue
                raise BybitError('Нет связи с Bybit. Проверьте регион и настройки ключа.') from None

    def post_once(self, path, data):
        """Submit once only. Network errors must never be retried blindly."""
        body = json.dumps(data, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        timestamp = str(int(time.time() * 1000))
        window = '10000'
        payload = timestamp + self.key + window + body.decode('utf-8')
        signature = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': self.key, 'X-BAPI-TIMESTAMP': timestamp,
                   'X-BAPI-RECV-WINDOW': window, 'X-BAPI-SIGN': signature,
                   'Content-Type': 'application/json'}
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except (ValueError, OSError):
                raise BybitUncertainError('Неизвестен результат запроса. Проверьте заявку на Bybit.') from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise BybitUncertainError('Неизвестен результат запроса. Проверьте заявку на Bybit.') from None
        if not isinstance(result, dict):
            raise BybitUncertainError('Неизвестен результат запроса. Проверьте заявку на Bybit.')
        code = result.get('retCode')
        if code != 0:
            raise BybitError('Bybit отклонил заявку: ' + str(code) + ' ' + str(result.get('retMsg', ''))[:160])
        return result.get('result') or {}

    def verify_read_only(self):
        info = self.get('/v5/user/query-api')
        if info.get('readOnly') != 1:
            raise BybitError('Ключ имеет права на изменение счёта. Создайте ключ только для чтения.')
        return {'uid': str(info.get('userID', '')), 'uta': info.get('uta'),
                'is_master': info.get('isMaster')}

    def verify_trade_subaccount(self):
        info = self.get('/v5/user/query-api')
        if info.get('isMaster') is not False or str(info.get('parentUid', '0')) == '0':
            raise BybitError('Нужен API-ключ именно субаккаунта, не основного аккаунта.')
        if info.get('readOnly') != 0:
            raise BybitError('Для ордеров нужен ключ субаккаунта с правом записи.')
        permissions = info.get('permissions') or {}
        if 'Order' not in (permissions.get('ContractTrade') or []):
            raise BybitError('У ключа нет права ContractTrade → Order.')
        unsafe_wallet = {'AccountTransfer', 'SubMemberTransfer', 'Withdraw'}
        if unsafe_wallet.intersection(permissions.get('Wallet') or []):
            raise BybitError('Уберите у ключа права на перевод и вывод средств.')
        return {'uid': str(info.get('userID', '')),
                'parent_uid': str(info.get('parentUid', '')),
                'ips': info.get('ips') or []}


def pages(client, path, params, list_key, cancelled=lambda: False):
    """Iterate all cursor pages and reject repeated cursors rather than truncate silently."""
    cursor = None
    seen = set()
    while True:
        if cancelled():
            return
        query = dict(params)
        if cursor:
            query['cursor'] = cursor
        result = client.get(path, query)
        rows = result.get(list_key)
        if not isinstance(rows, list):
            raise BybitError('Неожиданный формат ответа Bybit: ' + path)
        for row in rows:
            yield row
        next_cursor = result.get('nextPageCursor') or ''
        if not next_cursor:
            return
        if next_cursor in seen:
            raise BybitError('Bybit повторил страницу: ' + path)
        seen.add(next_cursor)
        cursor = next_cursor

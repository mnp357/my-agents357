import json
import os
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from coinglass_collector import collect
from liquidation_report import export

ROOT=Path('/var/lib/my-agents')
STATUS=ROOT/'job-status.json'
CANCEL=ROOT/'job-cancel'

def status(**values):
    values['updated_at']=datetime.now(timezone.utc).isoformat()
    tmp=STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(values,ensure_ascii=False))
    os.replace(tmp,STATUS)

def send_document(token,chat_id,path,caption):
    import secrets
    import urllib.request
    boundary='----agent'+secrets.token_hex(16)
    body=[]
    for key,value in [('chat_id',str(chat_id)),('caption',caption)]:
        body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    body.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\nContent-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n'.encode())
    body.extend([path.read_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
    req=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendDocument',data=b''.join(body),headers={'Content-Type':'multipart/form-data; boundary='+boundary})
    with urllib.request.urlopen(req,timeout=60) as response:
        result=json.load(response)
    if not result.get('ok'): raise RuntimeError('Не удалось отправить файл')

def run():
    from telegram_bridge import state,send
    current=state()
    if not current or current['owner_chat_id'] is None: raise RuntimeError('Telegram не связан')
    token,chat=current['token'],current['owner_chat_id']
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    reports=ROOT/'reports'
    reports.mkdir(exist_ok=True)
    raw=reports/(stamp+'.json')
    workbook=reports/('Hyperliquid-'+stamp+'.xlsx')
    status(state='running',message='Открываю CoinGlass',started_at=stamp)
    try:
        data=collect(raw,progress=lambda text:status(state='running',message=text,started_at=stamp),cancelled=CANCEL.exists)
        summary=export(data,workbook)
        caption=('Сбор остановлен. ' if data.get('cancelled') else 'Готово. ')+f"Проверено монет: {summary['checked']}/{summary['total']}. Позиций в пределах 7%: {summary['positions']}. До 3 страниц на монету, размер по убыванию, XYZ исключён."
        if summary['errors']: caption+=' Есть пропуски: смотрите лист «Охват».'
        send_document(token,chat,workbook,caption)
        status(state='cancelled' if data.get('cancelled') else 'completed',message=caption,file=workbook.name,**summary)
    except Exception:
        status(state='failed',message='Сбор или отправка не завершены. Повторите /activate; сохранённые файлы остаются на сервере.')
        try: send(token,chat,'Не удалось завершить сбор или отправку. CoinGlass мог не загрузить таблицу. Повторите /activate позже. Пустой результат не считается отсутствием ликвидаций.')
        except Exception: pass

if __name__=='__main__':
    with open(ROOT/'job.lock','a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        run()

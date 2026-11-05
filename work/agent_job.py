import json
import os
import fcntl
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from coinglass_collector import collect
from hyperliquid_entry import enrich
from liquidation_report import export
from wallet_history import load_history,append_history,save_history
from wallet_activity import connect as activity_connect,collect_activity,report_rows

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

def report_filename(started_at):
    local_time=started_at.astimezone(ZoneInfo('Asia/Omsk'))
    return 'Hyperliquid '+local_time.strftime('%d %m %Y %H-%M-%S')+'.xlsx'

def run():
    from telegram_bridge import state,send
    current=state()
    if not current or current['owner_chat_id'] is None: raise RuntimeError('Telegram не связан')
    token,chat=current['token'],current['owner_chat_id']
    started_at=datetime.now(timezone.utc)
    stamp=started_at.strftime('%Y%m%d-%H%M%S')
    reports=ROOT/'reports'
    reports.mkdir(exist_ok=True)
    raw=reports/(stamp+'.json')
    workbook=reports/report_filename(started_at)
    status(state='running',message='Открываю CoinGlass',started_at=stamp)
    stage='чтение CoinGlass'
    try:
        data=collect(raw,progress=lambda text:status(state='running',message=text,started_at=stamp),cancelled=CANCEL.exists)
        stage='проверка цен входа Hyperliquid'
        entry_coverage=enrich(data,progress=lambda text:status(state='running',message=text,started_at=stamp),cancelled=CANCEL.exists)
        raw.write_text(json.dumps(data,ensure_ascii=False,indent=2))
        stage='обновление истории кошельков'
        history_path=ROOT/'wallet-history.json'
        history=append_history(load_history(history_path),data)
        stage='проверка действий крупных кошельков'
        activity_db=activity_connect(ROOT/'wallet-activity.sqlite3')
        try:
            activity_summary=collect_activity(
                activity_db,history,data['started_at'],
                progress=lambda text:status(state='running',message=text,started_at=stamp),
                cancelled=CANCEL.exists)
            activity_events,activity_checks,activity_positions=report_rows(activity_db)
        finally:
            activity_db.close()
        if CANCEL.exists(): data['cancelled']=True
        stage='формирование Excel'
        summary=export(data,workbook,history_runs=history['runs'],
                       activity_events=activity_events,activity_checks=activity_checks,
                       activity_positions=activity_positions)
        save_history(history_path,history)
        caption=('Сбор остановлен. ' if data.get('cancelled') else 'Готово. ')+f"Проверено монет: {summary['checked']}/{summary['total']}. Позиций в пределах 7%: {summary['positions']}. До 3 страниц на монету, размер по убыванию, XYZ исключён."
        caption+=f" Цена входа Hyperliquid: {entry_coverage['matched']}/{entry_coverage['rows']} строк."
        caption+=f" Кошельков под наблюдением: {activity_summary['wallets']}; пробелов проверки: {activity_summary['gaps']}."
        if summary['errors']: caption+=' Есть пропуски: смотрите лист «Охват».'
        stage='отправка Excel в Telegram'
        send_document(token,chat,workbook,caption)
        status(state='cancelled' if data.get('cancelled') else 'completed',message=caption,file=workbook.name,raw_file=raw.name,**summary)
    except Exception as exc:
        # Exception text from Telegram may contain the bot token in its URL.
        # Keep only the stage and exception class in the status and message.
        message=f'Ошибка на этапе: {stage}. Тип: {type(exc).__name__}. Сохранённые файлы остаются на сервере.'
        status(state='failed',message=message,stage=stage,error_type=type(exc).__name__)
        try: send(token,chat,message+' Пустой результат не считается отсутствием ликвидаций.')
        except Exception: pass

if __name__=='__main__':
    with open(ROOT/'job.lock','a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(0)
        run()

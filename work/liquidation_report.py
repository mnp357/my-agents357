"""Excel export for the deployed Linux worker (standalone runtime)."""
from decimal import Decimal, InvalidOperation
import re
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo


ASSET_PRIORITY = {symbol: index for index, symbol in enumerate(
    ('BTC', 'ETH', 'HYPE', 'SOL'))}
SIDE_FILLS = {
    'Лонг': 'E7F4E8',
    'Шорт': 'FCE8EF',
}
# Match the owner's readable Excel layout for the «Позиции» sheet at 125% zoom.
POSITION_COLUMN_WIDTHS = {
    'A': 10, 'B': 10, 'C': 10, 'D': 10,
    'E': 10, 'F': 10, 'G': 39, 'H': 35.7, 'I': 50,
}
POSITION_ROW_HEIGHT = 22.5
POSITION_HEADER_HEIGHT = 76
TOP_POSITION_FILL = 'FFF2CC'
MONTHS = ('янв', 'фев', 'мар', 'апр', 'май', 'июн',
          'июл', 'авг', 'сен', 'окт', 'ноя', 'дек')


def asset_sort_key(symbol):
    """Keep the requested lead assets first, then sort the rest alphabetically."""
    name = str(symbol or '').upper()
    return (ASSET_PRIORITY.get(name, len(ASSET_PRIORITY)), name)


def format_entry_time(value):
    """CoinGlass displays entry time as HH:MM MM-DD, without year or zone."""
    text = str(value or '').strip()
    if not text:
        return None
    match = re.fullmatch(r'(\d{1,2}):(\d{2})\s+(\d{1,2})-(\d{1,2})', text)
    if not match:
        return text
    hour, minute, month, day = map(int, match.groups())
    if not (0 <= hour < 24 and 0 <= minute < 60 and 1 <= month <= 12 and 1 <= day <= 31):
        return text
    return f'{day:02d} {MONTHS[month - 1]}, {hour:02d}:{minute:02d}'

def number(text):
    text=str(text).replace('$','').replace(',','').replace('\u00a0','').replace(' ','').strip()
    multiplier=Decimal(1)
    if text[-1:] in ('K','M','B'):
        multiplier={'K':Decimal(1000),'M':Decimal(1000000),'B':Decimal(1000000000)}[text[-1]]
        text=text[:-1]
    try:
        value=Decimal(text)*multiplier
        return value if value.is_finite() else None
    except InvalidOperation:
        return None

def analyse(data):
    selected=[]
    coverage=[]
    for coin in data['coins']:
        missing=0
        for page in coin['pages']:
            for row in page['rows']:
                if 'XYZ' in row['symbol'].upper(): continue
                price,entry,liq,volume=(number(row.get(k,'')) for k in ('price','hyperliquid_entry_price','liquidation','volume'))
                if price is None or price<=0 or liq is None or liq<=0 or volume is None or volume<0:
                    missing+=1
                    continue
                distance=(liq-price)/price
                eligible=(row['side']=='Long' and -Decimal('.07')<=distance<=0) or (row['side']=='Short' and 0<=distance<=Decimal('.07'))
                if eligible:
                    wallet=str(row.get('wallet') or '')
                    if not re.fullmatch(r'0x[0-9a-fA-F]{40}',wallet): wallet=None
                    selected.append([row['symbol'],'Лонг' if row['side']=='Long' else 'Шорт',
                                     float(price),float(entry) if entry is not None else None,
                                     float(liq),float(distance),float(volume),page['captured_at'],
                                     wallet,row.get('hyperliquid_position_size')])
        coverage.append([coin['symbol'],len(coin['pages']),sum(len(p['rows']) for p in coin['pages']),missing,'Ошибка' if coin['error'] else 'Проверено',coin['error'] or ''])
    done={c['symbol'] for c in data['coins']}
    coverage.extend([[s,0,0,0,'Не проверено',''] for s in data['eligible_symbols'] if s not in done])
    selected.sort(key=lambda r:(r[0],r[1],abs(r[5])))
    return selected,coverage

def position_rows(data):
    """All captured positions, grouped by asset and then by descending USD size."""
    result=[]
    for coin in data['coins']:
        for page in coin['pages']:
            for row in page['rows']:
                if 'XYZ' in row.get('symbol','').upper():
                    continue
                volume=number(row.get('volume',''))
                entry=number(row.get('hyperliquid_entry_price',''))
                liquidation=number(row.get('liquidation',''))
                price=number(row.get('price',''))
                distance=(liquidation-price)/price if price and price>0 and liquidation and liquidation>0 else None
                result.append([row.get('symbol') or coin['symbol'],
                    {'Long':'Лонг','Short':'Шорт'}.get(row.get('side'),row.get('side','')),
                    float(volume) if volume is not None else None,
                    float(entry) if entry is not None else None,
                    float(liquidation) if liquidation is not None else None,
                    format_entry_time(row.get('entry_time')),
                    float(distance) if distance is not None else None])
    result.sort(key=lambda row: (asset_sort_key(row[0]), row[2] is None, -(row[2] or 0)))
    return result

def export(data,path,history_runs=None,activity_events=None,activity_checks=None,
           activity_positions=None):
    from openpyxl import Workbook
    from openpyxl.styles import Font,PatternFill,Alignment
    from openpyxl.utils import get_column_letter
    rows,coverage=analyse(data)
    wb=Workbook()
    ws=wb.active
    ws.title='Сводка'
    ws.append(['Hyperliquid — ликвидации в пределах 7%'])
    ws.append(['Источник',data['source']])
    ws.append(['Начало (UTC)',data['started_at']])
    ws.append(['Окончание (UTC)',data.get('finished_at','')])
    ws.append(['Охват','До 3 страниц каждой монеты, позиции по убыванию размера; XYZ исключён'])
    ws.append(['Ограничение','Последовательная выборка публичной таблицы; суммы и цены округлены CoinGlass. Лист «По размеру позиции» содержит все собранные строки, включая уровни дальше 7%.'])
    ws.append(['Примечание','Цена входа взята из открытой позиции Hyperliquid по адресу, активу и направлению на момент отдельного запроса; если позиция уже закрыта, изменила направление или ответ недоступен, ячейка пуста. Объём позиции не равен гарантированному объёму ликвидации. Пустые уровни пропущены на листе «Позиции»; семь крупнейших по размеру позиции строк выделены светло-жёлтым. На листе «По размеру позиции» месяц во времени входа показан тремя русскими буквами; год и часовой пояс CoinGlass не указывает.'])
    ws.append(['Результат','Неполный сбор' if any(c[4]!='Проверено' for c in coverage) or data.get('cancelled') else 'Все выбранные монеты проверены в пределах 3 страниц'])
    entry_coverage=data.get('hyperliquid_entry_coverage') or {}
    ws.append(['Цена входа (Hyperliquid)',
               f"Подтверждено: {entry_coverage.get('matched', 0)}/{entry_coverage.get('rows', 0)} строк; без адреса: {entry_coverage.get('missing_wallet', 0)}, несовпадение позиции: {entry_coverage.get('unmatched', 0)}, ошибка запроса: {entry_coverage.get('request_errors', 0)}, не проверено: {entry_coverage.get('not_checked', 0)}"])
    ws.append(['Монета','Лонги, USD','Шорты, USD',
               'Ближайший лонг','Ближайший шорт','Позиций в пределах 7%'])
    groups=defaultdict(list)
    for row in rows: groups[row[0]].append(row)
    for symbol in sorted(groups):
        long=[r for r in groups[symbol] if r[1]=='Лонг']
        short=[r for r in groups[symbol] if r[1]=='Шорт']
        nearest_long=min(long,key=lambda r:abs(r[5])) if long else None
        nearest_short=min(short,key=lambda r:abs(r[5])) if short else None
        ws.append([symbol,sum(r[6] for r in long),sum(r[6] for r in short),
                   nearest_long[4] if nearest_long else None,
                   nearest_short[4] if nearest_short else None,
                   len(groups[symbol])])
    detail=wb.create_sheet('Позиции')
    detail.append(['Монета','Сторона','Текущая цена, USD','Цена входа Hyperliquid, USD',
                   'Ликвидация, USD','Расстояние, %','Размер позиции, USD','Сбор (UTC)',
                   'Кошелёк Hyperliquid'])
    largest=set(sorted(range(len(rows)),key=lambda index:(-rows[index][6],index))[:7])
    highlighted=[]
    previous_symbol=None
    for index,row in enumerate(rows):
        if previous_symbol is not None and row[0]!=previous_symbol:
            detail.append([None]*9)
        detail.append(row[:9])
        if index in largest: highlighted.append(detail.max_row)
        previous_symbol=row[0]
    events_sheet=wb.create_sheet('История кошельков')
    events_sheet.append(['Действия и состояния крупных кошельков BTC и ETH'])
    events_sheet.append(['Исполнения взяты из публичной истории Hyperliquid с начала наблюдения. Закрытый PnL относится к конкретному исполнению, комиссия показана отдельно; фондирование и итог всей позиции здесь не учтены. Ликвидация отмечается только при явном признаке Hyperliquid. Строка проверки показывает охват, а не сделку.'])
    events_sheet.append([])
    events_sheet.append(['Время (Омск)','Метка кошелька','Монета','Событие','Направление',
                         'Размер исполнения, монет','Позиция после, монет','Цена, USD',
                         'Ликвидация, USD','Закрытый PnL, USDC','Открытый PnL, USD',
                         'Комиссия','Результат / охват','Источник / транзакция','Полный адрес'])
    activity_rows=[]
    for event in activity_events or []:
        if str(event.get('coin') or '').upper() not in ('BTC','ETH'):
            continue
        at=datetime.fromtimestamp(event['time_ms']/1000,ZoneInfo('Asia/Omsk')).replace(tzinfo=None)
        pnl=number(event.get('closed_pnl') or '')
        result=('Плюс по исполнению' if pnl is not None and pnl>0 else
                'Минус по исполнению' if pnl is not None and pnl<0 else
                'Нулевой PnL исполнения' if pnl is not None and
                 ('Закрыл' in event['event_type'] or 'Сократил' in event['event_type'] or
                  'Ликвидация' in event['event_type']) else '')
        if event.get('liquidation_method'):
            result=(result+'; ' if result else '')+'Ликвидация: '+event['liquidation_method']
        activity_rows.append((at,[at,event['name'],event['coin'],event['event_type'],
            event.get('direction'),number(event.get('size_coins') or ''),
            number(event.get('end_position') or ''),number(event.get('price') or ''),
            None,pnl,None,number(event.get('fee') or ''),result,
            (event.get('tx_hash') or '')+' / '+str(event.get('trade_id') or ''),
            event['address']]))
    for position in activity_positions or []:
        if str(position.get('coin') or '').upper() not in ('BTC','ETH'):
            continue
        at=datetime.fromisoformat(position['checked_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Omsk')).replace(tzinfo=None)
        size=number(position.get('signed_size') or '')
        side='Лонг' if size is not None and size>0 else 'Шорт' if size is not None and size<0 else ''
        activity_rows.append((at,[at,position['name'],position['coin'],'Снимок открытой позиции',
            side,None,size,number(position.get('entry_price') or ''),
            number(position.get('liquidation_price') or ''),None,
            number(position.get('unrealized_pnl') or ''),None,'',
            'Hyperliquid clearinghouseState',position['address']]))
    for check in activity_checks or []:
        at=datetime.fromisoformat(check['checked_at'].replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Omsk')).replace(tzinfo=None)
        status=f"Исполнения: {check['fills_status']}; позиции: {check['positions_status']}"
        if check.get('note'): status+='; '+check['note']
        activity_rows.append((at,[at,check['name'],None,'Проверка кошелька',None,
            None,None,None,None,None,None,None,status,
            f"Hyperliquid; исполнений: {check['fills_found']}",check['address']]))
    for _,record in sorted(activity_rows,key=lambda item:item[0],reverse=True):
        events_sheet.append(record)
    if events_sheet.max_row==4:
        events_sheet.append(['Наблюдение за исполнениями начнётся при следующем /activate'])
    history_sheet=wb.create_sheet('Снимки крупных')
    history_sheet.append(['История крупных кошельков'])
    history_sheet.append(['На каждом запуске: 7 крупнейших лонгов и 7 крупнейших шортов BTC и ETH в пределах 7%. Время в первой колонке — начало запуска; монеты собираются последовательно. Изменение количества монет — наблюдение, а не установленная причина сделки. Выход из топ-7 не означает закрытие позиции.'])
    history_sheet.append([])
    history_sheet.append(['Запуск (Омск)','Охват','Сторона','№','Метка кошелька',
                          'Монета','Позиция, USD','Количество монет','Δ монет',
                          'Цена входа','Ликвидация','Наблюдение','Полный адрес'])
    for run in reversed(history_runs or []):
        run_time=datetime.fromisoformat(run['run_at'].replace('Z','+00:00'))
        local_time=run_time.astimezone(ZoneInfo('Asia/Omsk')).replace(tzinfo=None)
        for item in run['observations']:
            if str(item.get('symbol') or '').upper() not in ('BTC','ETH'):
                continue
            qty=number(item.get('quantity') or '')
            delta=number(item.get('quantity_change') or '')
            history_sheet.append([
                local_time,'Полный' if run['complete'] else 'Частичный',
                item['side'],item['rank'],item['name'],item['symbol'],
                item['size_usd'],float(qty) if qty is not None else None,
                float(delta) if delta is not None else None,
                item['entry_price'],item['liquidation_price'],item['change'],item['wallet'],
            ])
    if history_sheet.max_row==4:
        history_sheet.append(['История появится после запуска /activate'])
    cov=wb.create_sheet('Охват')
    cov.append(['Монета','Страниц','Строк','Нет корректного уровня/числа','Статус','Ошибка'])
    for row in coverage: cov.append(row)
    size_sheet=wb.create_sheet('По размеру позиции')
    size_sheet.append(['Актив','Сторона','Позиция, USD','Цена входа Hyperliquid, USD',
                       'Цена ликвидации, USD','Время входа','Расстояние до ликвидации, %'])
    previous_symbol=None
    for row in position_rows(data):
        if previous_symbol is not None and row[0].upper()!=previous_symbol.upper():
            size_sheet.append([None]*7)
            size_sheet.append([None]*7)
        size_sheet.append(row)
        previous_symbol=row[0]
    for sheet,header in ((ws,10),(detail,1),(events_sheet,4),(history_sheet,4),(cov,1),(size_sheet,1)):
        sheet.freeze_panes=f'A{header+1}'
        sheet.auto_filter.ref=f'A{header}:{get_column_letter(sheet.max_column)}{max(header,sheet.max_row)}'
        sheet.sheet_view.showGridLines=False
        for cell in sheet[header]:
            cell.fill=PatternFill('solid',fgColor='18384A')
            cell.font=Font(color='FFFFFF',bold=True)
            cell.alignment=Alignment(wrap_text=True,vertical='center')
        sheet.row_dimensions[header].height=32
        for col in range(1,sheet.max_column+1): sheet.column_dimensions[get_column_letter(col)].width=23
    for row_number in highlighted:
        for cell in detail[row_number]:
            cell.fill=PatternFill('solid',fgColor=TOP_POSITION_FILL)
    ws.column_dimensions['B'].width=34
    ws.row_dimensions[1].height=30
    ws['A1'].font=Font(size=18,bold=True,color='18384A')
    for n in range(2,10): ws.merge_cells(start_row=n,start_column=2,end_row=n,end_column=6)
    for row in detail.iter_rows(min_row=2):
        for cell in row[2:5]: cell.number_format='#,##0.########'
        row[5].number_format='0.00%;[Red]-0.00%'
        row[6].number_format='#,##0.00'
    for column, width in POSITION_COLUMN_WIDTHS.items():
        detail.column_dimensions[column].width=width
    detail.sheet_format.defaultRowHeight=POSITION_ROW_HEIGHT
    detail.row_dimensions[1].height=POSITION_HEADER_HEIGHT
    detail.sheet_view.zoomScale=125
    events_sheet.merge_cells('A1:O1')
    events_sheet.merge_cells('A2:O2')
    events_sheet['A1'].font=Font(size=17,bold=True,color='18384A')
    events_sheet['A2'].alignment=Alignment(wrap_text=True,vertical='center')
    events_sheet.row_dimensions[1].height=29
    events_sheet.row_dimensions[2].height=42
    events_sheet.row_dimensions[4].height=42
    events_sheet.sheet_format.defaultRowHeight=23
    events_sheet.freeze_panes='D5'
    for column,width in {'A':23,'B':27,'C':14,'D':35,'E':18,'F':24,'G':25,
                         'H':22,'I':23,'J':23,'K':23,'L':19,'M':36,'N':70,'O':51}.items():
        events_sheet.column_dimensions[column].width=width
    for row in events_sheet.iter_rows(min_row=5):
        row[0].number_format='dd.mm.yyyy hh:mm:ss'
        kind=str(row[3].value or '')
        color=('FCE4D6' if 'Ликвидация' in kind or 'пробел' in str(row[12].value or '') else
               'E7F4E8' if any(word in kind for word in ('Открыл','Увеличил')) else
               'FCE8EF' if any(word in kind for word in ('Сократил','Закрыл','Развернул')) else
               'EDF3F7' if 'Снимок' in kind else 'F5F5F5')
        fill=PatternFill('solid',fgColor=color)
        for cell in row:
            cell.fill=fill
            cell.alignment=Alignment(horizontal='center',vertical='center')
        for index in (1,3,12,13,14):
            row[index].alignment=Alignment(horizontal='left',vertical='center')
        for index in range(5,12):
            row[index].number_format='#,##0.########'
    history_sheet.merge_cells('A1:M1')
    history_sheet.merge_cells('A2:M2')
    history_sheet['A1'].font=Font(size=17,bold=True,color='18384A')
    history_sheet['A2'].alignment=Alignment(wrap_text=True,vertical='center')
    history_sheet.row_dimensions[1].height=29
    history_sheet.row_dimensions[2].height=38
    history_sheet.row_dimensions[4].height=39
    history_sheet.sheet_format.defaultRowHeight=22.5
    history_sheet.freeze_panes='F5'
    for column,width in {'A':32,'B':15,'C':13,'D':8,'E':26,'F':14,
                         'G':23,'H':23,'I':20,'J':22,'K':22,'L':37,'M':51}.items():
        history_sheet.column_dimensions[column].width=width
    for row in history_sheet.iter_rows(min_row=5):
        row[0].number_format='dd.mm.yyyy hh:mm'
        color=SIDE_FILLS.get(row[2].value)
        if color:
            fill=PatternFill('solid',fgColor=color)
            for cell in row: cell.fill=fill
        for cell in row:
            cell.alignment=Alignment(horizontal='center',vertical='center')
        for index in (4,11,12):
            row[index].alignment=Alignment(horizontal='left',vertical='center')
        for index in (6,7,8,9,10):
            row[index].number_format='#,##0.########'
        row[8].number_format='+#,##0.########;-#,##0.########;0'
    cov.column_dimensions['D'].width=30
    cov.column_dimensions['F'].width=65
    size_sheet.column_dimensions['F'].width=28
    size_sheet.column_dimensions['G'].width=31
    size_sheet.column_dimensions['D'].width=28
    for row in size_sheet.iter_rows(min_row=2):
        for cell in row[2:5]: cell.number_format='#,##0.########'
        row[6].number_format='0.00%;[Red]-0.00%'
    for row in size_sheet.iter_rows():
        for cell in row:
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
    for row in size_sheet.iter_rows(min_row=2):
        color = SIDE_FILLS.get(row[1].value)
        if color:
            fill = PatternFill('solid', fgColor=color)
            for cell in row:
                cell.fill = fill
    for row in ws.iter_rows(min_row=11):
        for cell in row[1:5]: cell.number_format='#,##0.########'
    for row in ws.iter_rows(min_row=10):
        for cell in row:
            cell.alignment=Alignment(horizontal='center',vertical='center',wrap_text=True)
    wb.save(path)
    return {'positions':len(rows),'coins':len(groups),'checked':sum(c[4]=='Проверено' for c in coverage),'total':len(coverage),'errors':sum(c[4]!='Проверено' for c in coverage)}

from liquidation_report import analyse,number,export
from hyperliquid_entry import enrich
from pathlib import Path
from tempfile import TemporaryDirectory
from openpyxl import load_workbook

def row(symbol='XRP',side='Long',liq='93'):
    return dict(symbol=symbol,side=side,price='$100',liquidation=liq,volume='$1.20M')
data={'eligible_symbols':['XRP'],'coins':[{'symbol':'XRP','error':None,'pages':[{'captured_at':'2026-09-25T00:00:00Z','rows':[
    row(),row(liq='92.999'),row(side='Short',liq='107'),row(side='Short',liq='107.001'),row(symbol='xyz:GOLD'),row(liq='—'),row(liq='105')]}]}]}
selected,coverage=analyse(data)
assert len(selected)==2,selected
assert [r[1] for r in selected]==['Лонг','Шорт']
assert all(r[6]==1200000 for r in selected)
assert coverage[0][3]==1
assert number('NaN') is None
print('7% boundaries, direction, XYZ substring, missing values and amount parsing: passed')

with TemporaryDirectory() as directory:
    sample={'source':'CoinGlass','started_at':'2026-10-08T00:00:00Z',
            'finished_at':'2026-10-08T00:01:00Z','eligible_symbols':['BTC'],
            'coins':[{'symbol':'BTC','error':None,'pages':[{'captured_at':'2026-10-08T00:00:30Z','rows':[
                row('BTC','Long','94') | {'volume':'$10M','wallet':'0x'+'a'*40},
                row('BTC','Long','99') | {'volume':'$2M'},
                row('BTC','Short','106') | {'volume':'$8M'},
                row('BTC','Short','101') | {'volume':'$3M','price':'$101'},
            ]}]}]}
    def fake_account(wallet,dex):
        assert wallet == '0x'+'a'*40 and dex == ''
        return [{'position': {'coin':'BTC','szi':'2','entryPx':'95.25'}}]
    entry_coverage=enrich(sample,reader=fake_account,interval=0)
    assert entry_coverage['matched'] == 1
    assert entry_coverage['missing_wallet'] == 3
    file=Path(directory)/'report.xlsx'
    export(sample,file)
    sheet=load_workbook(file,read_only=True,data_only=True)['Сводка']
    assert sheet.cell(10,2).value == 'Лонги, USD'
    assert sheet.cell(11,2).value == 12000000
    assert [sheet.cell(10,col).value for col in (4,5,6)] == [
        'Ближайший лонг','Ближайший шорт','Позиций в пределах 7%']
    assert [sheet.cell(11,col).value for col in (4,5,6)] == [99,101,4]
    assert all(sheet.cell(row,col).alignment.horizontal == 'center'
               for row in (10,11) for col in range(1,7))
    positions=load_workbook(file,read_only=True,data_only=True)['Позиции']
    assert positions.cell(1,4).value == 'Цена входа Hyperliquid, USD'
    assert next(positions.cell(r,4).value for r in range(2,6)
                if positions.cell(r,5).value == 94) == 95.25
    assert next(positions.cell(r,4).value for r in range(2,6)
                if positions.cell(r,5).value == 99) is None
    all_positions=load_workbook(file,read_only=True,data_only=True)['По размеру позиции']
    assert all_positions.cell(2,4).value == 95.25
print('Summary nearest levels and centered alignment: passed')

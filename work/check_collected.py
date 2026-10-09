import json,sys
from liquidation_report import number,analyse
data=json.load(open(sys.argv[1]))
assert all('XYZ' not in s.upper() for s in data['eligible_symbols'])
for coin in data['coins']:
    rows=[r for p in coin['pages'] for r in p['rows']]
    assert len(coin['pages'])<=3
    assert all(r['symbol']==coin['symbol'] for r in rows)
    assert [int(r['rank']) for r in rows]==list(range(1,len(rows)+1))
    volumes=[number(r['volume']) for r in rows]
    assert all(a>=b for a,b in zip(volumes,volumes[1:])),coin['symbol']
selected,coverage=analyse(data)
assert all(abs(r[4])<=.07 for r in selected)
print(json.dumps({'coins':len(data['coins']),'eligible':len(data['eligible_symbols']),'pages':sum(len(c['pages']) for c in data['coins']),'rows':sum(len(p['rows']) for c in data['coins'] for p in c['pages']),'matches':len(selected),'errors':[c['symbol'] for c in data['coins'] if c['error']]},ensure_ascii=False))

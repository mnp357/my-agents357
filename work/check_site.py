import json
import urllib.request
import urllib.parse
import http.cookiejar

base='https://195.133.15.115'
creds=json.load(open('work/login.json'))
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
opener.open(base+'/login',urllib.parse.urlencode({'username':creds['user'],'password':creds['password']}).encode(),timeout=15).read()
with opener.open(base+'/api/agent/status',timeout=15) as response:
    data=json.load(response)
    print('authenticated status:',data['state'],data['message'])
if data.get('file'):
    with opener.open(base+'/reports/'+urllib.parse.quote(data['file']),timeout=15) as response:
        content=response.read()
        assert content[:2]==b'PK'
        print('authenticated Excel download:',len(content),'bytes')
with opener.open(base+'/app.js',timeout=15) as response:
    text=response.read().decode()
    assert 'до трёх страниц каждой монеты' in text
    print('deployed task description verified')

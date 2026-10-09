import json,subprocess
from pathlib import Path
ssh=['ssh','-i','work/timeweb_deploy','-o','UserKnownHostsFile=work/timeweb_known_hosts','root@195.133.15.115']
a=json.loads(Path('work/login.json').read_text())
subprocess.run(ssh+['htpasswd -icB /etc/nginx/my-agents.htpasswd roman'],input=a['password']+'\n',text=True,check=True,stdout=subprocess.DEVNULL)
config='''server {
 listen 80 default_server;
 listen [::]:80 default_server;
 server_name 9194583-tz838758.twc1.net;
 location ^~ /.well-known/acme-challenge/ { root /var/www/acme; }
 location / { return 403; }
}
'''
subprocess.run(ssh+['cat > /etc/nginx/sites-available/my-agents'],input=config,text=True,check=True)
subprocess.run(ssh+['chmod 640 /etc/nginx/my-agents.htpasswd; chown root:www-data /etc/nginx/my-agents.htpasswd; ln -sf /etc/nginx/sites-available/my-agents /etc/nginx/sites-enabled/my-agents; if test -L /etc/nginx/sites-enabled/default; then unlink /etc/nginx/sites-enabled/default; fi; nginx -t && systemctl reload nginx'],check=True)

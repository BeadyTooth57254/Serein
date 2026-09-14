"""Configure an authenticated gateway behind an existing Linux nginx and certificate."""
import hashlib
from pathlib import Path
import re
import subprocess


def host(value):
    value=value.strip().lower().rstrip('.')
    if len(value)>253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',value):
        raise ValueError('请填写已解析到本机的域名，不含协议、路径或端口。')
    return value


def closing_braces(text):
    stack=[];pairs={}
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\#[^\n]*|[{}]',text):
        token=match.group()
        if token=='{':stack.append(match.start())
        elif token=='}':
            if not stack:raise ValueError('nginx 配置括号不匹配。')
            pairs[stack.pop()]=match.end()
    if stack:raise ValueError('nginx 配置括号不匹配。')
    return pairs


def location(port,owner):
    if not 1<=int(port)<=65535:raise ValueError('网关端口无效。')
    return f'''location / {{
        # Serein public gateway {owner}
        proxy_pass http://127.0.0.1:{int(port)};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        client_max_body_size 64m;
    }}'''


def replace_placeholder(text,domain,port,owner):
    pairs=closing_braces(text);matches=[]
    for match in re.finditer(r'(?m)^\s*server\s*\{',text):
        start=match.end()-1;end=pairs[start];block=text[start:end]
        names=re.search(r'(?m)^\s*server_name\s+([^;]+);',block)
        if not names or domain not in names[1].split():continue
        if not re.search(r'(?m)^\s*listen\s+[^;]*\bssl\b',block):continue
        roots=list(re.finditer(r'(?m)^\s*location\s+/\s*\{',block))
        if len(roots)!=1:raise ValueError('已有 HTTPS 站点没有唯一的 location /，请先整理该站点。')
        root=roots[0];left=start+root.start();right=pairs[start+root.end()-1]
        previous=text[left:right]
        managed=f'# Serein public gateway {owner}' in previous
        placeholder=re.search(r'\breturn\s+(?:404|410)\s*;',previous) and not re.search(r'\b(?:proxy_pass|fastcgi_pass|try_files|alias|root)\b',previous)
        if not managed and not placeholder:raise ValueError('该域名首页已有内容，未覆盖；请使用空闲域名或先将首页设为 404/410。')
        matches.append((left,right))
    if len(matches)>1:raise ValueError('域名有多个 HTTPS server，无法自动选择。')
    if not matches:return None
    left,right=matches[0]
    return text[:left]+'\n    '+location(port,owner)+text[right:]


def plan(domain,port,deploy,config_dump):
    domain=host(domain);owner=hashlib.sha256(str(deploy.resolve()).encode()).hexdigest()[:12]
    paths=[Path(p) for p in re.findall(r'(?m)^# configuration file ([^\n]+):$',config_dump)]
    choices=[];domain_used=False
    for path in dict.fromkeys(paths):
        text=path.read_text()
        domain_used=domain_used or any(domain in m[1].split() for m in re.finditer(r'(?m)^\s*server_name\s+([^;]+);',text))
        changed=replace_placeholder(text,domain,port,owner)
        if changed is not None:choices.append((path.resolve(),changed))
    if len(choices)>1:raise ValueError('域名出现在多个 HTTPS 配置文件中，未修改。')
    if choices:return choices[0]
    if domain_used:raise ValueError('该域名已有站点，但没有可接入的 HTTPS 首页。请先配置证书。')
    cert=Path('/etc/letsencrypt/live')/domain
    if not (cert/'fullchain.pem').is_file() or not (cert/'privkey.pem').is_file():
        raise ValueError('未找到该域名的证书，请先用 Certbot 配置证书，或选择 IP／端口入口。')
    target=Path('/etc/nginx/conf.d')/f'serein-{owner}.conf'
    if target.exists():raise ValueError('同实例的 nginx 文件已存在但未加载，请先检查 nginx include。')
    return target,f'''server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}
server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate {cert}/fullchain.pem;
    ssl_certificate_key {cert}/privkey.pem;
    {location(port,owner)}
}}
'''


def apply(target,text,deploy,runner=subprocess.run):
    before=target.read_bytes() if target.exists() else None
    backups=deploy/'runtime'/'nginx-backups';backups.mkdir(parents=True,exist_ok=True,mode=0o700)
    if before is not None:
        backup=backups/(hashlib.sha256(before).hexdigest()+'.conf')
        if not backup.exists():backup.write_bytes(before);backup.chmod(0o600)
    temporary=target.with_name(target.name+'.serein-tmp')
    try:
        temporary.write_text(text);temporary.chmod(0o644);temporary.replace(target)
        runner(['nginx','-t'],check=True,capture_output=True)
        runner(['systemctl','reload','nginx'],check=True,capture_output=True)
    except Exception:
        if before is None:target.unlink(missing_ok=True)
        else:target.write_bytes(before)
        runner(['nginx','-t'],check=True,capture_output=True)
        runner(['systemctl','reload','nginx'],check=True,capture_output=True)
        raise ValueError('nginx 检查或重载失败，已恢复之前的配置。') from None

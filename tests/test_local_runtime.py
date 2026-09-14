"""Real Python core + built Node gateway, isolated data and loopback ports."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest


ROOT=Path(__file__).parents[1]


def link_directory(target,link):
    if os.name=='nt':
        env=dict(os.environ,SEREIN_TEST_TARGET=str(target),SEREIN_TEST_LINK=str(link))
        subprocess.run(['powershell','-NoProfile','-Command',
            'New-Item -ItemType Junction -Path $env:SEREIN_TEST_LINK -Target $env:SEREIN_TEST_TARGET | Out-Null'],
            env=env,check=True,capture_output=True)
    else:link.symlink_to(target,target_is_directory=True)


def test_native_services_auth_persistence_restart_and_port_conflict(tmp_path):
    if not shutil.which('node') or not (ROOT/'web'/'dist'/'client'/'index.html').exists() or not (ROOT/'web'/'node_modules').exists():
        pytest.skip('Run npm ci and npm run build in web before the native service integration test')
    root=tmp_path/'含空格 instance';deploy=root/'deploy';runtime=deploy/'runtime';secrets=deploy/'secrets'
    runtime.mkdir(parents=True);secrets.mkdir()
    (root/'scripts').mkdir()
    shutil.copy2(ROOT/'scripts'/'local_runtime.py',root/'scripts'/'local_runtime.py')
    web=root/'web';web.mkdir()
    for name in ('vite.config.mjs','package.json'):
        shutil.copy2(ROOT/'web'/name,web/name)
    shutil.copytree(ROOT/'web'/'server',web/'server')
    link_directory(ROOT/'web'/'node_modules',web/'node_modules')
    link_directory(ROOT/'web'/'dist',web/'dist')
    sockets=[socket.socket() for _ in range(3)]
    for item in sockets:item.bind(('127.0.0.1',0))
    gateway,memory,preview=[item.getsockname()[1] for item in sockets]
    for item in sockets:item.close()
    (deploy/'installation.json').write_text(json.dumps({'backend':'local','memory_port':memory,'preview_port':preview}))
    (deploy/'.env').write_text(f'SEREIN_BIND=127.0.0.1\nSEREIN_PORT={gateway}\n')
    text=(ROOT/'config.example.toml').read_text('utf-8').replace('./.runtime/','./runtime/')
    (deploy/'config.toml').write_text(text,'utf-8')
    (secrets/'api-token').write_text('synthetic-native-token')
    salt=b'fixture-native-salt';password='synthetic-page-password'
    auth={'username':'fixture','salt':salt.hex(),'hash':hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32).hex()}
    (secrets/'web-auth.json').write_text(json.dumps(auth))
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONUTF8='1')
    def run(*args,check=True):
        result=subprocess.run([sys.executable,str(root/'scripts'/'local_runtime.py'),'--deploy',str(deploy),*args],
            env=env,capture_output=True,text=True,encoding='utf-8',timeout=100)
        if check and result.returncode:
            logs='\n'.join(p.read_text('utf-8',errors='replace')[-5000:] for p in (runtime/'logs').glob('*.log'))
            pytest.fail(result.stdout+result.stderr+logs)
        return result
    base=f'http://127.0.0.1:{gateway}'
    basic='Basic '+base64.b64encode(f'fixture:{password}'.encode()).decode()
    def request(path='/',auth=basic,data=None):
        headers={'Authorization':auth}
        if data is not None:headers.update({'Content-Type':'application/json','Origin':base})
        req=Request(base+path,headers=headers,data=json.dumps(data).encode() if data is not None else None,method='PATCH' if data is not None else 'GET')
        try:
            with urlopen(req,timeout=10) as response:return response.status,response.read()
        except HTTPError as exc:return exc.code,exc.read()
    try:
        run('start')
        record=(runtime/'memory.control.json').read_bytes()
        run('start')
        assert (runtime/'memory.control.json').read_bytes()==record
        assert request(auth='')[0]==401
        status,page=request();assert status==200 and b'synthetic-native-token' not in page
        assert request('/v1/models')[0]==401
        assert request('/v1/models',auth='Bearer synthetic-native-token')[0]==200
        assert request('/__serein/settings',data={'identity':{'user_name':'Native Fixture'}})[0]==200
        run('restart','memory')
        assert json.loads(request('/__serein/settings')[1])['identity']['user_name']=='Native Fixture'
        run('restart','gateway')
        assert json.loads(request('/__serein/settings')[1])['identity']['user_name']=='Native Fixture'
        run('stop')
        assert not (runtime/'memory.control.json').exists()
        assert not (runtime/'gateway.control.json').exists()
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1',memory));occupied.listen()
            failed=run('start','memory',check=False)
            assert failed.returncode!=0
            # Failure must not terminate the unrelated listener or claim success.
            with socket.create_connection(('127.0.0.1',memory),timeout=2):pass
        run('start')
        assert json.loads(request('/__serein/settings')[1])['identity']['user_name']=='Native Fixture'
    finally:
        run('stop',check=False)

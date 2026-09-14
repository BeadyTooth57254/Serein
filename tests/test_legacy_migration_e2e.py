"""Synthetic upstream only. Also runnable directly in a Linux verification venv."""
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading

from serein.bootstrap import initialize
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.configured_models import effective_settings, prepare_selected
from serein.legacy_migration.scan import scan
from serein.legacy_migration.workflow import Migration
from serein.legacy_migration.vectors import maintain
from serein.recall.legacy_indexes import refresh


def test_full_synthetic_conversion(tmp_path):
    counts=Counter()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/embeddings'):
                counts['embedding']+=1
                texts=data['input'] if isinstance(data['input'],list) else [data['input']]
                result={'model':'mock','data':[{'index':i,'embedding':[1.,0.,0.,0.]} for i,_ in enumerate(texts)]}
            else:
                content=json.loads(data['messages'][1]['content'])
                prompt=data['messages'][0]['content']
                if 'forbidden_names' in content:
                    counts['tag']+=1
                    result={'domain':'life','cues':['周六书店约定'],'entities':[]}
                elif 'passages' in content:
                    counts['binding']+=1
                    p=content['passages'][0]
                    result={'bindings':[{'cue':c,'passage_ordinal':p['ordinal'],'evidence':p['text'][:40],'confidence':0.95} for c in content['cues']]}
                elif 'legacy_hint' in content:
                    counts['edge']+=1;result={'edges':[]}
                else:raise AssertionError('Unexpected model request: '+prompt[:50])
                result={'choices':[{'message':{'content':json.dumps(result)}}]}
            body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        old=tmp_path/'old';folder=old/'buckets'/'dynamic';folder.mkdir(parents=True)
        body='周六，他们如约在青禾书店见面。'+'沿着书架寻找上次提到的小说。'*50
        (folder/'a.md').write_text('---\nid: a\nname: 书店约定\n---\n### moment\n'+body+'\n### reflection\n需要删掉。\n### affect_anchor\n也需要删掉。','utf-8')
        settings=Settings(tmp_path/'new'/'memory.db',tmp_path/'new'/'index.db',writable=True);initialize(settings)
        url=f'http://127.0.0.1:{server.server_port}/v1'
        save_settings(settings.database,{'recall':{'passages_enabled':True},'models':[{'id':'mock','label':'合成模型','model':'mock','base_url':url,'protocol':'openai'}],
            'assignments':{'operit_tagging':'mock','embedding':'mock','reranker':'mock'}})
        plan=scan(old);options={'user_name':'Mira','ai_name':'Sol','aliases':[]}
        if sys.platform=='linux':
            config=tmp_path/'config.toml';config.write_text(f'[storage]\ndatabase="{settings.database}"\nindex="{settings.index}"\n[runtime]\nwritable=true\n','utf-8')
            options_file=tmp_path/'options.json';options_file.write_text(json.dumps(options),'utf-8')
            args=[sys.executable,'-m','serein.legacy_migration','--config',str(config),'wizard',str(old),'--options',str(options_file)]
            for _ in range(2):
                result=subprocess.run(args,input='yes\nyes\nyes\nyes\nyes\n',text=True,capture_output=True,timeout=180)
                assert result.returncode==0,result.stdout+'\n'+result.stderr
            import fcntl
            with (settings.database.parent/'migrations'/'operation.lock').open('a+') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                result=subprocess.run(args,capture_output=True,text=True,timeout=15)
                assert result.returncode!=0 and '不要双开' in result.stderr
        else:
            migration=Migration(settings,plan,options)
            try:
                migration.backup();migration.freeze_configuration();migration.import_bodies()
                asyncio.run(migration.tag_all());prepare_selected(settings)
                asyncio.run(migration.edges());result=asyncio.run(refresh(effective_settings(settings)))
                assert not result['cues'].get('failed_scenes'),result
                migration.import_bodies();asyncio.run(migration.tag_all())
            finally:migration.close()
        assert counts['tag']==1 and counts['binding']==1 and counts['embedding']>1,counts
        effective=effective_settings(settings)
        with Store(settings.database) as store:
            assert store.conn.execute('SELECT count(*) FROM documents').fetchone()[0]==1
            assert store.conn.execute('SELECT body_md FROM revisions ORDER BY number DESC LIMIT 1').fetchone()[0]==body
        with sqlite3.connect(effective.index) as conn:
            assert conn.execute('SELECT count(*) FROM passages WHERE embedding IS NOT NULL').fetchone()[0]>1
            assert conn.execute('SELECT count(*) FROM vectors').fetchone()[0]==1
        assert maintain(settings,'clean')['canonical_writes']==0
        print('SYNTHETIC_MIGRATION_OK',dict(counts))
    finally:server.shutdown();server.server_close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='serein-migration-smoke-') as folder:test_full_synthetic_conversion(Path(folder))

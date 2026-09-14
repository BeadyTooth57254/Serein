from test_live_clients import live
from serein.core.store import Store, digest, encode


def test_read_archives_keeps_deleted_dream_as_metadata_only(live):
    settings, client=live
    with Store(settings.database) as store, store.transaction():
        for key, surfaced in (('dream_kept',True),('dream_deleted',False)):
            meta={'dream_id':key,'generated_at':'2026-09-07T00:00:00Z','local_date':'2026-09-07','ai_name':'Assistant','surfaced':surfaced}
            store.conn.execute('INSERT INTO historical_works VALUES (?,?,?,?,?,?,?,?,?)',
                (key,'dream',1,key,'梦的正文',digest('梦的正文'),encode(meta),'test',key+'.md'))
            store.conn.execute('INSERT INTO historical_work_events VALUES (?,?,?,?,?)',
                ('test',1 if surfaced else 2,key,'surfaced' if surfaced else 'deleted',encode(meta)))
        store.record_deletion('dream_deleted','2026-09-07T00:00:00Z',{})
    records=client.get('/api/dreams').json()['records']
    kept=next(row for row in records if row['dream_id']=='dream_kept')
    deleted=next(row for row in records if row['dream_id']=='dream_deleted')
    assert kept['status']=='surfaced' and kept['has_body'] is True
    assert deleted['status']=='forgotten' and deleted['has_body'] is False
    assert all('body' not in row for row in records)
    assert client.get('/api/dreams/dream_kept').json()['body']=='梦的正文'
    assert client.get('/api/dreams/dream_deleted').status_code==404
    assert client.get('/api/window-shadows').status_code==404

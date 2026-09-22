"""Creation-time candidate filtering, without a model or a private deployment."""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from serein.api.settings import PipelinePatch
from serein.extensions import pipeline


NOW=datetime(2026,9,23,12,tzinfo=timezone.utc)


@pytest.fixture
def catalog(monkeypatch):
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    conn.executescript('''
        CREATE TABLE fact_events(item_id TEXT PRIMARY KEY,status TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE pipeline_track_events(track_id TEXT,event_id TEXT);
        CREATE TABLE fact_event_sources(id INTEGER PRIMARY KEY,item_id TEXT,source_system TEXT,session_id TEXT,
            message_id TEXT,role TEXT,content TEXT,created_at TEXT);
        CREATE TABLE raw_events(id INTEGER PRIMARY KEY,source TEXT,session_id TEXT,source_event_id TEXT);
        CREATE TABLE pipeline_event_details(event_id TEXT,details_json TEXT);
    ''')
    class ReadStore:
        def __init__(self,database,*,read_only=False):
            assert read_only
        def __enter__(self):return SimpleNamespace(conn=conn)
        def __exit__(self,*args):return False
    monkeypatch.setattr(pipeline,'Store',ReadStore)
    monkeypatch.setattr(pipeline,'reference_blockers',lambda conn,key:[])
    def add(key,created,*,status='active',track='track',updated=None,source_date='2020-01-01T00:00:00Z'):
        conn.execute('INSERT INTO fact_events VALUES (?,?,?,?)',(key,status,created,updated or created))
        conn.execute('INSERT INTO pipeline_track_events VALUES (?,?)',(track,key))
        conn.execute('INSERT INTO fact_event_sources(item_id,source_system,session_id,message_id,role,content,created_at) '
                     'VALUES (?,\'synthetic\',\'window\',?,\'user\',?,?)',(key,key,'complete original '+key,source_date))
    yield conn,add
    conn.close()


def selected(window):
    return pipeline.candidates('unused',['track'],window=window)


def test_default_window_is_exactly_72_hours():
    window=pipeline.base_event_window({},clock=NOW)
    assert window['lookback_days']==3
    assert datetime.fromisoformat(window['created_after'])==NOW-timedelta(hours=72)
    assert datetime.fromisoformat(window['created_before'])==NOW


@pytest.mark.parametrize('days',[1,7,30,365])
def test_configurable_window(days):
    window=pipeline.base_event_window({'base_event_lookback_days':days},clock=NOW)
    assert datetime.fromisoformat(window['created_after'])==NOW-timedelta(days=days)
    assert PipelinePatch(base_event_lookback_days=days).base_event_lookback_days==days


@pytest.mark.parametrize('days',[0,-1,366,True,3.5,'3'])
def test_invalid_days_are_rejected(days):
    with pytest.raises(ValueError):pipeline.base_event_window({'base_event_lookback_days':days},clock=NOW)
    with pytest.raises(ValidationError):PipelinePatch(base_event_lookback_days=days)


def test_creation_not_updated_or_source_activity(catalog):
    conn,add=catalog
    add('old','2026-09-01T00:00:00Z',updated=NOW.isoformat(),source_date=NOW.isoformat())
    add('new','2026-09-22T00:00:00Z')
    rows=selected(pipeline.base_event_window({},clock=NOW))
    assert [row['event_id'] for row in rows]==['new']
    assert rows[0]['originals'][0]['content']=='complete original new'
    assert rows[0]['originals'][0]['created_at']=='2020-01-01T00:00:00Z'
    assert conn.execute("SELECT status FROM fact_events WHERE item_id='old'").fetchone()[0]=='active'
    assert conn.execute('SELECT count(*) FROM fact_event_sources').fetchone()[0]==2


def test_inclusive_boundaries_offsets_and_microseconds(catalog):
    _,add=catalog
    add('lower','2026-09-20T12:00:00Z')
    add('offset','2026-09-20T20:00:00+08:00')
    add('upper',NOW.isoformat())
    add('too_old','2026-09-20T11:59:59.999999Z')
    add('future','2026-09-23T12:00:00.000001Z')
    assert {row['event_id'] for row in selected(pipeline.base_event_window({},clock=NOW))}=={'lower','offset','upper'}


def test_lifecycle_and_track_filters_remain(catalog):
    _,add=catalog
    for status in ('archived','superseded','tombstoned'):
        add(status,NOW.isoformat(),status=status)
    add('foreign',NOW.isoformat(),track='other')
    add('active',NOW.isoformat())
    assert [row['event_id'] for row in selected(pipeline.base_event_window({},clock=NOW))]==['active']


def test_wider_setting_admits_older_creation(catalog):
    _,add=catalog
    add('four_days',(NOW-timedelta(days=4)).isoformat())
    assert selected(pipeline.base_event_window({},clock=NOW))==[]
    assert len(selected(pipeline.base_event_window({'base_event_lookback_days':7},clock=NOW)))==1


def test_excluded_originals_are_not_loaded(catalog):
    conn,add=catalog
    add('old','2026-01-01T00:00:00Z')
    calls=[]
    conn.set_trace_callback(calls.append)
    assert selected(pipeline.base_event_window({},clock=NOW))==[]
    assert not any('FROM fact_event_sources' in sql or 'FROM raw_events' in sql for sql in calls)


def setup_component(monkeypatch):
    original={'id':10,'session_id':1,'content':'new dialogue','created_at':NOW.isoformat()}
    assignments=[{'source_message_id':10,'primary_track_id':'track','context_track_ids':[],'routing_role':'primary_activity'}]
    monkeypatch.setattr(pipeline,'route_result',lambda data,routed:(assignments,[{'track_id':'track'}],1))
    return {'messages':[original],'routing_messages':[original],
            'input_policy':{'base_event_lookback_days':3}}


def test_component_freezes_window_and_reads_full_selected_originals(catalog,monkeypatch):
    _,add=catalog
    add('recent','2026-09-22T00:00:00Z')
    data=setup_component(monkeypatch)
    original_window=pipeline.base_event_window
    monkeypatch.setattr(pipeline,'base_event_window',lambda policy:original_window(policy,clock=NOW))
    first=pipeline.components('unused',data,{})
    frozen=dict(data['base_event_window'])
    def must_not_recompute(*args,**kwargs):raise AssertionError('frozen window changed')
    monkeypatch.setattr(pipeline,'base_event_window',must_not_recompute)
    data['input_policy']['base_event_lookback_days']=1
    second=pipeline.components('unused',data,{})
    assert data['base_event_window']==frozen
    assert first==second
    assert first[0]['base_event_window']==frozen
    assert {m['content'] for m in first[0]['context_messages']}=={'new dialogue','complete original recent'}


def test_ownership_only_recheck_does_not_build_or_load_materials(monkeypatch):
    data=setup_component(monkeypatch)
    def forbidden(*args,**kwargs):raise AssertionError('ownership check loaded material')
    monkeypatch.setattr(pipeline,'base_event_window',forbidden)
    monkeypatch.setattr(pipeline,'candidates',forbidden)
    result=pipeline.components('unused',data,{},include_materials=False)
    assert 'base_event_window' not in data
    assert result[0]['base_event_candidates']==[]


def test_direct_candidate_call_uses_configured_default(catalog,monkeypatch):
    _,add=catalog
    add('recent','2026-09-22T00:00:00Z')
    add('old','2026-09-01T00:00:00Z')
    window=pipeline.base_event_window
    monkeypatch.setattr(pipeline,'base_event_window',lambda policy:window(policy,clock=NOW))
    monkeypatch.setattr(pipeline,'read_settings',lambda database:{'pipeline':{'base_event_lookback_days':3}})
    assert [row['event_id'] for row in pipeline.candidates('unused',['track'])]==['recent']

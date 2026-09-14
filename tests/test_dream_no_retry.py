"""Synthetic checks: one paid attempt per dream day, including failed requests."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from serein.compat.dreams import Dreams
from serein.compat.germany.dream_engine import DreamEngine
from test_live_clients import live


def ready(settings):
    engine=Dreams(settings)
    engine.client=object()
    engine.enabled=engine.auto_enabled=True
    engine.daily_probability=1
    materials=[{'id':'synthetic','content':'Synthetic material','metadata':{}}]*5
    engine.select_materials=AsyncMock(return_value=materials)
    engine._payload_for=lambda *args: {'synthetic': True}
    engine._call_dream_model=AsyncMock(return_value='Synthetic dream text.')
    return engine


@pytest.mark.parametrize('failure',['request','cancelled','save'])
def test_failed_dream_stays_stopped_after_restart_but_next_day_is_new(live,failure):
    settings,_=live
    engine=ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=engine.tz)
    error=asyncio.CancelledError if failure=='cancelled' else ValueError
    if failure=='save':
        def failed_save(*args): raise ValueError('Synthetic save failure')
        engine._write_record=failed_save
    else:
        engine._call_dream_model.side_effect=error('Synthetic request failure')
    with pytest.raises(error): asyncio.run(engine.generate(None,now=stamp))
    assert engine._call_dream_model.await_count==1
    for worker in (engine,ready(settings)):
        result=asyncio.run(worker.generate(None,now=stamp+timedelta(hours=1)))
        assert result['reason']=='daily_attempt_already_started'
    restarted=ready(settings)
    result=asyncio.run(restarted.generate(None,now=stamp+timedelta(days=1)))
    assert result['status']=='created'
    assert restarted._call_dream_model.await_count==1
    assert asyncio.run(restarted.generate(None,now=stamp+timedelta(days=1,hours=1)))['status']=='exists'
    assert restarted._call_dream_model.await_count==1


def test_competing_dream_workers_cannot_issue_two_requests(live):
    settings,_=live
    first,second=ready(settings),ready(settings)
    stamp=datetime(2026,9,14,4,tzinfo=first.tz)
    async def run():
        both=asyncio.Event(); arrivals=0
        async def materials(*args):
            nonlocal arrivals
            arrivals+=1
            if arrivals==2: both.set()
            await both.wait()
            rows=[{'id':'synthetic','content':'Synthetic material','metadata':{}}]*5
            return rows
        first.select_materials=second.select_materials=materials
        return await asyncio.gather(first.generate(None,now=stamp),second.generate(None,now=stamp))
    results=asyncio.run(run())
    assert sum(result['status']=='created' for result in results)==1
    assert first._call_dream_model.await_count+second._call_dream_model.await_count==1


def test_dream_sdk_does_not_retry_http_errors(monkeypatch):
    import openai
    for name in ('SEREIN_DREAM','OMBRE_DREAM'):
        for suffix in ('ENABLED','API_KEY','MODEL','BASE_URL'):
            monkeypatch.delenv(name+'_'+suffix,raising=False)
    requests=[]
    def response(request):
        requests.append(request)
        return httpx.Response(503,json={'error':{'message':'Synthetic unavailable'}})
    original=openai.AsyncOpenAI
    monkeypatch.setattr(openai,'AsyncOpenAI',lambda **kwargs: original(
        **kwargs,http_client=httpx.AsyncClient(transport=httpx.MockTransport(response))))
    engine=DreamEngine({'dream':{'enabled':True,'api_key':'synthetic',
        'base_url':'https://synthetic.invalid/v1','model':'synthetic'}})
    engine._dream_prompt=lambda:'Synthetic prompt'
    async def run():
        try:
            with pytest.raises(openai.APIStatusError): await engine._call_dream_model({})
        finally: await engine.client.close()
    asyncio.run(run())
    assert len(requests)==1

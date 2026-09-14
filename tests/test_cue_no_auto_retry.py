"""A failed cue binder request must not become a polling-loop model bill."""

import asyncio

from serein.recall.germany.memory_recall.cue_passage_shadow import CuePassageShadowIndex


class Binder:
    model = 'synthetic-binder'

    def __init__(self):
        self.calls = 0
        self.fail = True

    async def bind(self, **_materials):
        self.calls += 1
        await asyncio.sleep(.02)
        if self.fail:
            raise TimeoutError('synthetic provider timeout')
        return {'bindings': [{'cue': 'window', 'passage_ordinal': 0,
                              'evidence': 'window', 'confidence': .9}]}


class Embedding:
    enabled = True
    model = 'synthetic-embedding'
    document_instruction = ''

    async def embed_document(self, _text):
        return [1.0]


def materials(text='window near the rain'):
    return ([{'id': 'scene_test', 'title': 'Test', 'cues': ['window']}],
            {('scene', 'scene_test'): [{'ordinal': 0, 'start_offset': 0,
                                      'end_offset': len(text), 'text': text}]})


def test_failed_binding_is_paused_across_polls_and_restart(tmp_path):
    binder = Binder()
    config = {'state_dir': str(tmp_path)}

    async def run():
        index = CuePassageShadowIndex(config, Embedding(), binder=binder)
        scenes, passages = materials()
        first = await index.sync(scenes=scenes, passages_by_owner=passages)
        assert first['failed_scenes'] == ['scene_test:TimeoutError']
        restarted = CuePassageShadowIndex(config, Embedding(), binder=binder)
        second = await restarted.sync(scenes=scenes, passages_by_owner=passages)
        assert binder.calls == 1
        assert second['paused_scenes'] == ['scene_test:TimeoutError']
        assert (await restarted.sync(scenes=scenes, passages_by_owner=passages,
                                     dry_run=True))['to_bind'] == 0

        binder.fail = False
        recovered = await restarted.sync(scenes=scenes, passages_by_owner=passages,
                                         retry_failed=True)
        assert recovered['status'] == 'ok'
        assert binder.calls == 2
        assert (await restarted.sync(scenes=scenes, passages_by_owner=passages))['reused_scenes'] == 1
        assert binder.calls == 2

        changed_scenes, changed_passages = materials('window near the sea')
        await restarted.sync(scenes=changed_scenes, passages_by_owner=changed_passages)
        assert binder.calls == 3

    asyncio.run(run())


def test_competing_workers_claim_only_one_provider_request(tmp_path):
    binder = Binder()
    index = CuePassageShadowIndex({'state_dir': str(tmp_path)}, Embedding(), binder=binder)

    async def run():
        scenes, passages = materials()
        await asyncio.gather(*(index.sync(scenes=scenes, passages_by_owner=passages)
                               for _ in range(2)))
        assert binder.calls == 1

    asyncio.run(run())


def test_interrupted_request_is_not_reissued_on_restart(tmp_path):
    class InterruptedBinder(Binder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()

        async def bind(self, **_materials):
            self.calls += 1
            self.started.set()
            await asyncio.Event().wait()

    binder = InterruptedBinder()
    config = {'state_dir': str(tmp_path)}

    async def run():
        scenes, passages = materials()
        index = CuePassageShadowIndex(config, Embedding(), binder=binder)
        task = asyncio.create_task(index.sync(scenes=scenes, passages_by_owner=passages))
        await binder.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        restarted = CuePassageShadowIndex(config, Embedding(), binder=binder)
        report = await restarted.sync(scenes=scenes, passages_by_owner=passages)
        assert binder.calls == 1
        assert report['paused_scenes'] == ['scene_test:interrupted']

    asyncio.run(run())


def test_old_inflight_result_cannot_clear_new_source_failure(tmp_path):
    class OrderedBinder(Binder):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def bind(self, **_materials):
            self.calls += 1
            if self.calls == 1:
                self.started.set()
                await self.release.wait()
                return {'bindings': [{'cue': 'window', 'passage_ordinal': 0,
                                      'evidence': 'window', 'confidence': .9}]}
            raise TimeoutError('synthetic changed-source failure')

    binder = OrderedBinder()
    index = CuePassageShadowIndex({'state_dir': str(tmp_path)}, Embedding(), binder=binder)

    async def run():
        old_scenes, old_passages = materials()
        new_scenes, new_passages = materials('window near the sea')
        first = asyncio.create_task(index.sync(scenes=old_scenes, passages_by_owner=old_passages))
        await binder.started.wait()
        failed = await index.sync(scenes=new_scenes, passages_by_owner=new_passages)
        assert failed['failed_scenes'] == ['scene_test:TimeoutError']
        binder.release.set()
        await first
        paused = await index.sync(scenes=new_scenes, passages_by_owner=new_passages)
        assert paused['paused_scenes'] == ['scene_test:TimeoutError']
        assert binder.calls == 2

    asyncio.run(run())


def test_legacy_cue_and_scene_clients_disable_sdk_retries(monkeypatch):
    from importlib import import_module
    import openai
    from serein.compat.germany.scene_linker import SceneLinker
    from serein.recall.germany.memory_recall.cue_passage_shadow import DeepSeekCuePassageBinder

    calls = []

    def client(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(openai, 'AsyncOpenAI', client)
    monkeypatch.setattr(import_module('serein.recall.germany.memory_recall.cue_passage_shadow'),
                        'AsyncOpenAI', client)
    DeepSeekCuePassageBinder({'cue_passage_shadow': {
        'api_key': 'synthetic', 'base_url': 'https://example.invalid', 'binding_model': 'test'}})
    SceneLinker._load_providers({'models': [{'name': 'test', 'model': 'test',
                                            'base_url': 'https://example.invalid',
                                            'api_key': 'synthetic'}]}, {})
    assert len(calls) == 2
    assert all(call['max_retries'] == 0 for call in calls)

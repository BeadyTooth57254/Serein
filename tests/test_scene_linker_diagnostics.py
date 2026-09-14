import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from serein.compat.germany.scene_linker import SceneLinker


def response_client(content, finish_reason='stop'):
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=AsyncMock(return_value=response))))


def call(linker, content, finish_reason='stop'):
    provider = {'name': 'synthetic', 'model': 'synthetic', 'client': response_client(content, finish_reason),
                'token_parameter': 'max_tokens', 'max_tokens': 200}
    payload = {'new_scene': {'scene_id': 'scene:synthetic'}}
    return asyncio.run(linker._call_provider(provider, payload))


def test_invalid_relation_response_logs_safe_shape_by_default(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('SEREIN_SCENE_LINKER_LOG_INVALID_RESPONSE', raising=False)
    linker = SceneLinker({'serein_database': str(tmp_path / 'synthetic.db'), 'scene_linker': {}})
    with caplog.at_level(logging.WARNING, logger='serein_brain.scene_linker'):
        assert call(linker, 'Synthetic private memory', 'length') is None
    assert 'reason=not_json_object' in caplog.text
    assert 'finish_reason=length' in caplog.text
    assert 'content_chars=24' in caplog.text
    assert 'Synthetic private memory' not in caplog.text


def test_relation_response_raw_debug_is_explicit_and_only_for_invalid_output(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('SEREIN_SCENE_LINKER_LOG_INVALID_RESPONSE', '1')
    linker = SceneLinker({'serein_database': str(tmp_path / 'synthetic.db'), 'scene_linker': {}})
    with caplog.at_level(logging.WARNING, logger='serein_brain.scene_linker'):
        assert call(linker, '{"wrong":"Synthetic private memory"}') == {'wrong': 'Synthetic private memory'}
    assert 'reason=missing_edges' in caplog.text
    assert 'Synthetic private memory' in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='serein_brain.scene_linker'):
        assert call(linker, '{"edges":[]}') == {'edges': []}
    assert not caplog.records

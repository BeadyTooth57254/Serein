import json

import pytest

httpx = pytest.importorskip("httpx")

from serein.adapters.reranker import RerankerClient, RerankerProviderError
from serein.config import load_settings
from serein.recall.service import Recall


def test_configured_reranker_preserves_question_and_maps_provider_positions(tmp_path, monkeypatch):
    profile = tmp_path / "config.toml"
    profile.write_text('''[storage]
database = "runtime.db"
[reranker]
endpoint = "https://rerank.example/v1/rerank"
model = "configured-model"
api_key_env = "SEREIN_TEST_RERANKER_KEY"
''', encoding="utf-8")
    from serein.core.store import Store
    settings = load_settings(profile)
    with Store(settings.database):pass
    engine = Recall(settings)
    monkeypatch.setenv("SEREIN_TEST_RERANKER_KEY", "test-secret")
    question = "世界之窗那天发生了什么？"
    documents = [{"ref": "event:first", "title": "归航", "body": "原始经历"},
                 {"ref": "scene:second", "title": "窗前", "body": "场景正文"}]
    def handle(request):
        payload = json.loads(request.content)
        assert payload == {"model": "configured-model", "query": question,
                           "documents": ["归航\n原始经历", "窗前\n场景正文"],
                           "top_n": 2, "return_documents": False}
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": .9},
                                                     {"index": 0, "relevance_score": .2}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert engine.reranker(question, documents, client=client) == {"scene:second": .9, "event:first": .2}


def test_reranker_failures_cannot_turn_into_admission(monkeypatch):
    monkeypatch.setenv("SEREIN_TEST_RERANKER_KEY", "test-secret")
    scorer = RerankerClient("https://rerank.example/v1/rerank", "configured-model", "SEREIN_TEST_RERANKER_KEY")
    docs = [{"ref": "event:first", "title": "标题", "body": "正文"}]
    failures = [httpx.Response(401, text="private test-secret"),
                httpx.Response(200, json={"results": [{"index": 7, "relevance_score": .9}]}),
                httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.9}]}),
                httpx.Response(200, json={"results": [{"index": 0, "relevance_score": .9}] * 2})]
    for response in failures:
        with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
            with pytest.raises(ValueError) as error:
                scorer("问题", docs, client=client)
            assert "test-secret" not in str(error.value)


@pytest.mark.parametrize('failure,code',[(401,'http_401'),(429,'http_429'),('timeout','timeout')])
def test_provider_errors_have_safe_diagnostic_codes(failure,code):
    def handle(request):
        if failure=='timeout':raise httpx.ReadTimeout('private response detail',request=request)
        return httpx.Response(failure,text='private response detail')
    scorer=RerankerClient('https://rerank.example/v1/rerank','test-model',api_key='test-secret')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(RerankerProviderError) as error:
            scorer('question',[{'ref':'scene:a','title':'A','body':'B'}],client=client)
    assert error.value.code==code
    assert 'private' not in str(error.value) and 'test-secret' not in str(error.value)

"""The public Hook example must distinguish prepared cards from actual delivery."""
import io
import json

from examples.hook_host import SereinHook


def test_example_prepares_once_and_acknowledges_only_after_host_success(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        assert timeout == 15
        assert request.get_header("Authorization") == "Bearer synthetic-key"
        path = request.full_url.split("https://serein.example", 1)[1]
        body = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), path, body))
        if path.startswith("/v1/host/deliveries?"):
            result = {"status": "ok", "has_more": False, "items": [
                {"window_id": "chat-001", "reported_by": "serein_chat_proxy", "delivered_ids": ["scene:other"]},
                {"window_id": "chat-001", "reported_by": "host", "delivered_ids": ["scene:old"]},
                {"window_id": "another-window", "reported_by": "host", "delivered_ids": ["scene:unrelated"]},
            ]}
        elif path == "/api/hook/recall":
            result = {"ok": True, "injected": False, "recalled_ids": ["event:new"],
                      "additional_context": "Synthetic memory context"}
        else:
            assert path == "/v1/host/deliveries"
            result = {"status": "recorded", "delivered_ids": body["delivered_ids"]}
        return io.BytesIO(json.dumps(result).encode())

    monkeypatch.setattr("examples.hook_host.urlopen", fake_urlopen)
    client = SereinHook("https://serein.example", "synthetic-key")
    source = [{"role": "user", "content": "New question"}]
    turn = client.prepare("chat-001", source)
    assert source == [{"role": "user", "content": "New question"}]
    assert "Synthetic memory context" in turn.messages[-1]["content"]
    assert turn.delivered_ids == ["event:new"]
    assert calls[1][2]["delivered_ids"] == ["scene:old"]
    assert len(calls) == 2  # Looking up memory alone does not record delivery.
    assert client.record_success(turn)["status"] == "recorded"
    assert calls[2][2] == {"receipt_id": turn.receipt_id, "window_id": "chat-001",
                           "delivered_ids": ["event:new"]}

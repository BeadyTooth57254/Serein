import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "examples" / "codex-continuity-packet" / "prepare_codex_window.py"
SPEC = importlib.util.spec_from_file_location("codex_continuity_example", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_example_preserves_serein_original_and_upstream_message_ids():
    packet = json.loads((SCRIPT.parent / "packet.example.json").read_text(encoding="utf-8"))
    merged = MODULE.merge_resume_pages([packet])
    items = MODULE.build_history_items(merged)

    assert [item["role"] for item in items] == [
        "developer", "developer", "developer", "developer", "user", "assistant"
    ]
    user_text = items[-2]["content"][0]["text"]
    assistant_text = items[-1]["content"][0]["text"]
    assert '"id":"raw:41"' in user_text
    assert '"source_message_id":"message-0041"' in user_text
    assert user_text.endswith("新窗口里也要能查回这句话的出处。")
    assert '"id":"raw:42"' in assistant_text
    assert items[-1]["content"][0]["type"] == "output_text"


def test_example_assembles_paged_bodies_and_rejects_gaps():
    first = {
        "collection_id": "same",
        "items": [{"id": "event:one", "kind": "event", "body_md": "first ",
                   "body_offset": 0, "body_complete": False}],
    }
    second = {
        "collection_id": "same",
        "items": [{"id": "event:one", "kind": "event", "body_md": "second",
                   "body_offset": 6, "body_complete": True}],
    }
    assert MODULE.merge_resume_pages([first, second])["items"][0]["body_md"] == "first second"
    second["items"][0]["body_offset"] = 7
    with pytest.raises(ValueError, match="missing or out of order"):
        MODULE.merge_resume_pages([first, second])

    with pytest.raises(ValueError, match="still has more data"):
        MODULE.merge_resume_pages([{**first, "has_more": True}])

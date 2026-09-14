import base64
from copy import deepcopy
import json

import pytest
import yaml

from serein.core import Conflict, Store
from serein.core.store import digest
from serein.ingest.legacy_scene import import_snapshot


def artifact(path, raw):
    return {"path": path, "bytes_b64": base64.b64encode(raw).decode(), "sha256": digest(raw)}


@pytest.fixture
def snapshot():
    meta = {"id": "scene_old", "name": "旧标题", "write_contract": "write-scene-v1",
            "scene_revision": 2, "scene_status": "archived", "author": "user",
            "scene_revision_history": [{"revision": 1, "title": "最初", "content": "旧正文", "cues": ["线索"]}]}
    raw = ("---\n" + yaml.safe_dump(meta, allow_unicode=True) + "---\n\n当前 **正文**\r\n").encode()
    evidence = {"id": 3, "scene_id": "scene_old", "source_system": "chat", "session_id": "s",
                "message_id": "7", "role": "user", "content": "原文", "content_sha256": digest("原文")}
    return {"format": "serein-scene-snapshot-v1", "origin": "synthetic-test", "captured_at": "2026-01-01",
            "scenes": [artifact("archive/scene.md", raw)], "evidence": [evidence],
            "evidence_actions": [{"id": 8, "evidence_id": 3, "action": "unbind", "actor": "user", "created_at": "2026-01-02"}],
            "tombstones": [artifact(".tombstones/deleted.json", json.dumps({"id": "scene_deleted", "deleted_at": "2026-01-01"}).encode())]}


def test_import_keeps_original_ids_revisions_unbinds_deletions_and_is_idempotent(tmp_path, snapshot):
    with Store(tmp_path / "test.db") as store:
        first = import_snapshot(store, snapshot)
        counts = {t: store.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ["documents", "revisions", "sources", "evidence_bindings", "evidence_actions", "import_records"]}
        second = import_snapshot(store, snapshot)
        assert first["inserted_scenes"] == 1
        assert second["inserted_scenes"] == second["inserted_bindings"] == 0
        assert store.read("scene_old")["body_md"] == "\n当前 **正文**\r\n"
        assert store.read("scene_old")["lifecycle"] == "archived"
        assert store.read("scene_old", revision=1)["body_md"] == "旧正文"
        assert store.conn.execute("SELECT active FROM evidence_bindings").fetchone()[0] == 0
        for table, count in counts.items():
            assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
        with pytest.raises(Conflict):
            store.create("scene_deleted", "scene", "deleted", "body")


def test_bad_evidence_rolls_back_entire_import(tmp_path, snapshot):
    snapshot["evidence"][0]["content"] = "changed after capture"
    with Store(tmp_path / "test.db") as store:
        with pytest.raises(ValueError, match="Evidence checksum"):
            import_snapshot(store, snapshot)
        assert store.read("scene_old") is None
        assert store.conn.execute("SELECT count(*) FROM import_records").fetchone()[0] == 0


def test_existing_origin_cannot_be_silently_replaced(tmp_path, snapshot):
    with Store(tmp_path / "test.db") as store:
        import_snapshot(store, snapshot)
        changed = deepcopy(snapshot)
        changed["captured_at"] = "later"
        with pytest.raises(Conflict, match="source changed"):
            import_snapshot(store, changed)
        assert store.read("scene_old")["title"] == "旧标题"

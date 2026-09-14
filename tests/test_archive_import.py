import base64
from copy import deepcopy
import json
import sqlite3

import pytest

from serein.core import Conflict, Store
from serein.core.reader import Reader
from serein.core.store import digest, encode
from serein.ingest.archive_snapshot import export_archives
from serein.ingest.legacy_archive import import_archives


def artifact(path, raw):
    return {"path": path, "bytes_b64": base64.b64encode(raw).decode(), "sha256": digest(raw)}


@pytest.fixture
def archive_snapshot():
    dream = b"---\r\ndream_id: dream_one\r\nsource_bucket_ids: [scene_old]\r\nsurfaced: true\r\n---\r\noriginal dream\r\n"
    deleted = {"dream_id": "dream_deleted", "event": "deleted", "deleted_at": "old", "reason": "surfaced_one_shot"}
    return {"format": "serein-archive-snapshot-v1", "origin": "test-archives", "captured_at": "2026-09-06T00:00:00+00:00",
            "window_shadows": [{"window_id": "window_one", "revision_number": 2, "content": "shadow body",
                                "source_hash": "original-input-hash", "sections_json": '{ "section": "body" }',
                                "moment_bucket_ids_json": '["scene_deleted"]', "revision_root_id": "window_one"}],
            "files": [artifact("dreams/dream_one.md", dream),
                      artifact("dreams/logs/events.jsonl", encode(deleted).encode()+b"\n"),
                      artifact("dreams/old.backup", b"Never a new dream")]}


def test_archives_preserve_original_fields_and_deleted_dreams_without_runtime(tmp_path, archive_snapshot):
    path = tmp_path / "test.db"
    with Store(path) as store:
        store.record_deletion("scene_deleted", "old", {})
        report = import_archives(store, archive_snapshot)
        assert report["kinds"] == {"shadow": 1, "dream": 1}
        assert report["dream_event_counts"] == {"deleted": 1}
        assert report["reference_states"] == {"historical": 1, "deleted": 1, "not_imported_or_missing": 1}
        assert not report["handoff_enabled"] and not report["dream_generation_enabled"]
        assert store.conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert import_archives(store, archive_snapshot)["inserted_works"] == 0
        assert store.conn.execute("SELECT count(*) FROM historical_work_events").fetchone()[0] == 1
    with Reader(path) as reader:
        shadow = reader.read("shadow:window_one", revision=2)
        assert shadow["document"]["metadata"] == archive_snapshot["window_shadows"][0]
        assert not shadow["surface_state"]["can_surface"]
        assert reader.read("dream_one")["document"]["body_md"] == "original dream\r\n"
        assert reader.read("dream:dream_deleted")["status"] == "deleted"
        assert reader.read("window_one", revision=1)["status"] == "revision_missing"
        assert reader.store.conn.total_changes == 0


def test_late_artifact_error_rolls_back_works_and_keeps_other_documents(tmp_path, archive_snapshot):
    archive_snapshot["files"][-1]["sha256"] = "wrong"
    with Store(tmp_path / "test.db") as store:
        store.create("scene_existing", "scene", "Scene", "original")
        with pytest.raises(ValueError, match="checksum"):
            import_archives(store, archive_snapshot)
        assert store.conn.execute("SELECT count(*) FROM historical_works").fetchone()[0] == 0
        assert store.conn.execute("SELECT count(*) FROM deletions").fetchone()[0] == 0
        assert store.read("scene_existing")["body_md"] == "original"


def test_changed_snapshot_cannot_replace_an_archived_work(tmp_path, archive_snapshot):
    with Store(tmp_path / "test.db") as store:
        import_archives(store, archive_snapshot)
        changed = deepcopy(archive_snapshot)
        changed["origin"] = "another-export"
        changed["window_shadows"][0]["content"] = "different body"
        with pytest.raises(Conflict):
            import_archives(store, changed)
        assert store.conn.execute("SELECT body_md FROM historical_works WHERE id='window_one'").fetchone()[0] == "shadow body"


def test_export_does_not_change_sources_or_create_missing_database(tmp_path):
    (tmp_path / "dreams/logs").mkdir(parents=True)
    (tmp_path / "dreams/dream_one.md").write_bytes(b"original bytes\r\n")
    (tmp_path / "dreams/logs/events.jsonl").write_bytes(b"{}\n")
    with sqlite3.connect(tmp_path / "window_shadows.sqlite") as db:
        db.execute("CREATE TABLE window_shadows (window_id TEXT,content TEXT)")
        db.execute("INSERT INTO window_shadows VALUES ('window_one','original')")
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = export_archives(tmp_path)
    assert len(result["window_shadows"]) == 1 and len(result["files"]) == 2
    assert all(p.read_bytes()==raw for p,raw in before.items())
    with pytest.raises(sqlite3.OperationalError):
        export_archives(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()

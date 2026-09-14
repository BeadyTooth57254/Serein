import base64
from copy import deepcopy
import json
import sqlite3

import pytest

from serein.core import Conflict, Store
from serein.core.notebook import read_entry, resolve_entry
from serein.core.store import digest, encode
from serein.ingest.diary_snapshot import export_diaries
from serein.ingest.legacy_diary import import_diaries


def artifact(path, raw):
    return {"path": path, "bytes_b64": base64.b64encode(raw).decode(), "sha256": digest(raw)}


@pytest.fixture
def diary_snapshot():
    def entry(i, kind="diary", author="ai", deleted="", unlock="", source=""):
        return {"id": i, "entry_type": kind, "revision": 2, "author": author, "date": "2026-05-04",
                "title": None, "content": f"original {i}\r\n", "visibility": "deleted" if deleted else "active",
                "unlock_at": unlock, "deleted_at": deleted, "created_at": "original creation",
                "updated_at": "original update", "source_id": source, "metadata": '{ "key": 1 }',
                "emotion_tags": '["original tag"]'}
    rows = [entry(1), entry(2, author="user", deleted="old deletion"),
            entry(3, "darkroom", unlock="2026-05-05T00:00:00+08:00"),
            entry(4, "darkroom", deleted="old deletion", source="legacy_darkroom:dr_old")]
    old = encode({"id": "dr_old", "note": rows[3]["content"], "visibility": "active"}).encode() + b"\n"
    return {"format": "serein-diary-snapshot-v1", "origin": "test-diaries", "captured_at": "2026-05-04T12:00:00+08:00",
            "diaries": rows,
            "comments": [{"id": 1, "diary_id": 1, "author": "user", "content": "comment", "created_at": "old"},
                         {"id": 2, "diary_id": 2, "author": "ai", "content": "deleted owner comment", "created_at": "old"}],
            "diary_revisions": [{"id": 1, "diary_id": 2, "revision": 1, "content": "prior body", "author": "user", "reason": "delete"},
                                {"id": 2, "diary_id": 99, "revision": 1, "content": "absent owner's body"}],
            "darkroom_sessions": [{"id": 1, "diary_id": 3, "locked_at": "2026-05-04T12:00:00+08:00", "unlock_at": "2026-05-05T00:00:00+08:00"}],
            "legacy_files": [artifact("darkroom/entries.jsonl", old), artifact("darkroom/state.json", b'{"last_entry_id":"dr_old"}')],
            "absent_legacy_files": []}


def test_authors_comments_history_and_deleted_legacy_owner_survive(tmp_path, diary_snapshot):
    with Store(tmp_path / "test.db") as store:
        report = import_diaries(store, diary_snapshot)
        result = read_entry(store, "diary-vps-1", at=diary_snapshot["captured_at"])
        assert result["entry"] == diary_snapshot["diaries"][0]
        assert result["comments"] == diary_snapshot["comments"][:1]
        assert read_entry(store, 2)["entry"] is None
        assert read_entry(store, "dr_old")["resolution"] == "deleted"
        assert report["legacy_checks"][0]["content_exact"]
        assert report["legacy_checks"][0]["resolution"] == "deleted"
        assert report["owner_resolutions"]["missing"] == 1
        assert store.conn.execute("SELECT 1 FROM diary_entries WHERE id=99").fetchone() is None
        assert store.conn.execute("SELECT body_md FROM diary_history WHERE id=2").fetchone()[0] == "absent owner's body"
        assert import_diaries(store, diary_snapshot)["inserted_entries"] == 0
        assert store.conn.execute("SELECT count(*) FROM diary_comments").fetchone()[0] == 2


def test_lock_hides_prose_and_metadata_without_changing_storage(tmp_path, diary_snapshot):
    with Store(tmp_path / "test.db") as store:
        import_diaries(store, diary_snapshot)
        changes = store.conn.total_changes
        locked = read_entry(store, 3, at="2026-05-04T16:00:00+09:00")
        assert locked["resolution"] == "locked" and locked["entry"] is None
        assert "original 3" not in encode(locked)
        assert read_entry(store, 3, at="2026-05-04T16:00:00+00:00")["entry"] is not None
        assert store.conn.total_changes == changes
        assert resolve_entry(store, 3, kind="diary")["resolution"] == "wrong_kind"


def test_ledger_alias_requires_id_date_and_exact_body_and_stays_snapshot_bound(tmp_path, diary_snapshot):
    with Store(tmp_path / "test.db") as store:
        sha = digest(diary_snapshot["diaries"][0]["content"])
        body = f"| 2026-05-04 | Assistant-Diary `#1` title | `diary_source_20260504_exact` | `{sha}` | source |\n"
        body += f"| 2026-05-03 | Assistant-Diary `#1` title | `diary_source_wrong_date` | `{sha}` | source |\n"
        store.create("narrative_one", "narrative", "Narrative", body)
        report = import_diaries(store, diary_snapshot)
        assert [p["date_and_body_exact"] for p in report["source_ledger_checks"]] == [True, False]
        assert read_entry(store, "diary_source_20260504_exact")["entry_id"] == 1
        assert resolve_entry(store, "diary_source_wrong_date")["resolution"] == "unresolved_legacy_source"
        store.conn.execute("UPDATE diary_entries SET body_md='new version' WHERE id=1")
        assert read_entry(store, "diary_source_20260504_exact")["resolution"] == "historical_source"


def test_late_session_conflict_rolls_back_notebook_batch(tmp_path, diary_snapshot):
    with Store(tmp_path / "test.db") as store:
        store.conn.execute("INSERT INTO diary_sessions VALUES (1,NULL,'other','other','{}')")
        with pytest.raises(Conflict, match="diary_sessions"):
            import_diaries(store, diary_snapshot)
        for table in ("diary_entries", "diary_comments", "diary_history", "import_records"):
            assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_changed_snapshot_cannot_restore_deleted_entries(tmp_path, diary_snapshot):
    with Store(tmp_path / "test.db") as store:
        import_diaries(store, diary_snapshot)
        changed = deepcopy(diary_snapshot)
        changed["origin"] = "new-export"
        changed["diaries"][1].update(visibility="active", deleted_at="")
        with pytest.raises(Conflict):
            import_diaries(store, changed)
        assert resolve_entry(store, 2)["resolution"] == "deleted"


def test_export_is_read_only_including_soft_deletions_and_optional_legacy_files(tmp_path):
    path = tmp_path / "diary.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE diaries (id INTEGER,deleted_at TEXT)")
        db.executemany("INSERT INTO diaries VALUES (?,?)", [(1,""),(2,"deleted")])
        for table in ("comments", "diary_revisions", "darkroom_sessions"):
            db.execute(f"CREATE TABLE {table} (id INTEGER)")
    before = path.read_bytes()
    exported = export_diaries(tmp_path)
    assert len(exported["diaries"]) == 2
    assert len(exported["absent_legacy_files"]) == 2
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        export_diaries(tmp_path / "missing")
    assert not (tmp_path / "missing").exists()

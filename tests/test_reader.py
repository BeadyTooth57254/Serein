import base64
import json
import sqlite3

import pytest

from serein.core import Store
from serein.core.reader import Reader
from serein.core.store import digest, encode
from serein.ingest.legacy_diary import import_diaries
from test_diary_import import diary_snapshot


@pytest.fixture
def read_database(tmp_path, diary_snapshot):
    path = tmp_path / "read.db"
    with Store(path) as store:
        store.create("scene_a", "scene", "Scene", "current scene", revision=2)
        store._add_revision("scene_a", 1, "old title", "old scene", {}, "old")
        store.create("scene_deleted", "scene", "secret title", "deleted secret")
        store.set_lifecycle("scene_deleted", "deleted")
        store.record_deletion("scene_gone", "old", {"private": "not exposed"})
        store.create("event_a", "event", "Archived event", "archived original", lifecycle="archived", manual_surface=False)
        store.create("event_live", "event", "Event", "live original")
        first = store.add_source("message/1", "bound source")
        store.bind("scene_a", first)
        store.bind("event_a", first)
        inactive = store.add_source("message/2", "unbound source")
        store.bind("scene_a", inactive, active=False)
        store.create("narrative_a", "narrative", "Narrative", "body", revision=2,
                     metadata={"legacy_registry": {"arc_key": "arc_a", "publication_status": "reviewed"}})
        store._add_revision("narrative_a", 1, "Old Narrative", "old body", {"legacy_registry": {"arc_key": "arc_a"}}, "old")
        refs = [("scene", "scene_a", "linked"), ("scene", "scene_a", "mentioned"),
                ("scene", "scene_deleted", "linked"), ("event", "event_missing", "linked"),
                ("event", "event_a", "linked"), ("event", "event_live", "excluded"),
                ("diary", "1", "linked"), ("diary", "diary-vps-1", "mentioned"),
                ("darkroom", "3", "linked"), ("scene", "scene_mention", "mentioned"),
                ("upload", "upload_a", "linked")]
        for kind, target, disposition in refs:
            store.conn.execute("INSERT INTO narrative_materials VALUES (?,2,?,?,?,?,?)",
                               ("narrative_a", disposition, kind, target, disposition, "{}"))
        for event in ["event_live", "event_a"]:
            store.conn.execute("INSERT INTO event_arc_links VALUES ('arc_a',?,'original','{}')", (event,))
        raw = b"\xff\x00original\r\n"
        store.save_import_record("origin", "original.bin", raw)
        store.conn.execute("INSERT INTO narrative_uploads VALUES ('upload_a','origin','original.bin',?)",
                           (encode({"filename": "original.bin", "extracted_text": "text view"}),))
        import_diaries(store, diary_snapshot)
    return path


def test_explicit_archived_read_retains_body_evidence_and_does_not_surface(read_database):
    with Reader(read_database) as reader:
        result = reader.read("event:event_a")
        assert result["readable"] and result["status"] == "archived"
        assert result["document"]["body_md"] == "archived original"
        assert result["evidence"][0]["content"] == "bound source"
        assert not result["surface_state"]["can_surface"]
        assert reader.read("scene_a")["evidence"] == reader.read("scene:scene_a")["evidence"]
        assert len(reader.read("scene_a")["evidence"]) == 1
        assert reader.store.conn.total_changes == 0
        with pytest.raises(sqlite3.OperationalError):
            reader.store.set_manual_surface("event_a", True)


def test_deleted_wrong_type_and_history_never_substitute_current_body(read_database):
    with Reader(read_database) as reader:
        for target in ["scene_deleted", "scene_gone"]:
            result = reader.read(target, revision=1)
            assert result["status"] == "deleted" and result["document"] is None
            assert "secret" not in encode(result)
        assert reader.read("scene_a", kind="event")["status"] == "wrong_kind"
        old = reader.read("scene_a", revision=1)
        assert old["document"]["body_md"] == "old scene"
        assert old["evidence"] == [] and old["evidence_scope"] == "unavailable_for_revision"
        assert reader.read("scene_a", revision=99)["status"] == "revision_missing"


def test_notebook_comments_lock_and_upload_original_bytes(read_database):
    with Reader(read_database) as reader:
        diary = reader.read("diary:1")
        assert diary["document"]["author"] == "ai"
        assert diary["comments"][0]["author"] == "user"
        assert reader.read("diary-vps-1")["document"] == diary["document"]
        locked = reader.read("darkroom:3", at="2026-05-04T12:00:00+08:00")
        assert locked["status"] == "locked" and locked["document"] is None and locked["comments"] == []
        assert reader.read("diary:2", revision=1)["status"] == "deleted"
        upload = reader.read("upload:upload_a", include_blob=True)
        assert base64.b64decode(upload["bytes_b64"]) == b"\xff\x00original\r\n"
        assert upload["document"]["body_format"] == "extracted_text"
        assert "bytes_b64" not in reader.read("upload_a")


def test_material_exclusion_beats_append_dedup_and_mentions_stay_separate(read_database):
    with Reader(read_database) as reader:
        result = reader.materials("narrative_a", limit=100, with_evidence=True, at="2026-05-04T12:00:00+08:00")
        items = {i["id"]: i for i in result["items"]}
        assert items["event_live"]["selection"] == "excluded" and items["event_live"]["object"] is None
        assert items["scene_mention"]["selection"] == "mention_only"
        assert items["event_a"]["object"]["readable"]
        assert len(items["event_a"]["references"]) == 2
        assert items["scene_deleted"]["object"]["status"] == "deleted"
        assert items["event_missing"]["object"]["status"] == "missing"
        assert items["3"]["object"]["status"] == "locked"
        assert len(items["1"]["requested_ids"]) == 2
        assert len(items["scene_a"]["object"]["evidence"]) == 1
        assert len(result["items"]) == 9
        mentioned = reader.materials("narrative_a", limit=100, include_mentions=True)
        assert next(i for i in mentioned["items"] if i["id"] == "scene_mention")["object"]["status"] == "missing"


def test_historical_materials_do_not_receive_current_arc_appends_and_pagination_is_stable(read_database):
    with Reader(read_database) as reader:
        assert reader.materials("narrative_a", revision=1)["items"] == []
        first = reader.materials("narrative_a", limit=4)
        second = reader.materials("narrative_a", offset=first["next_offset"], limit=100)
        all_items = reader.materials("narrative_a", limit=100)["items"]
        assert first["items"] + second["items"] == all_items
        assert second["next_offset"] is None


def test_reader_never_creates_or_upgrades_input_database(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        Reader(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()
    path = tmp_path / "older.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=4")
        db.execute("PRAGMA application_id=1397902897")
    with pytest.raises(ValueError, match="schema v5"):
        Reader(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4

from copy import deepcopy
import json
import sqlite3

import pytest

from serein.core import Conflict, Store
from serein.core.store import digest, encode
from serein.ingest.event_snapshot import export_events
from serein.ingest.legacy_event import import_events


@pytest.fixture
def event_snapshot():
    def event(name, status="active", recallable=1):
        return {"item_id": name, "item_type": "event", "title": name, "body": "original\r\nbody",
                "status": status, "recallable": recallable, "created_at": "created", "updated_at": "updated",
                "covered_by_scene_id": "", "supersedes_item_id": ""}
    a, b = event("event_a"), event("event_b", "archived", None)
    a["supersedes_item_id"] = "event_missing"
    b["covered_by_scene_id"] = "scene_one"
    sources = [{"id": i, "item_id": "event_a", "source_system": "assistant_bridge", "session_id": "1",
                "thread_id": "thread", "message_id": str(i), "content": body, "content_sha256": digest(body),
                "hash_algorithm": "sha256-utf8", "evidence_kind": kind}
               for i, body, kind in [(1, "primary words", "primary"), (2, "supporting words", "supporting")]]
    receipt = {"operation_id": "operation_one", "request_sha256": "original_request_hash", "created_at": "old",
               "result_json": '{ "ok": true, "items": [{"item_id":"event_a"}] }'}
    return {"format": "serein-event-snapshot-v1", "origin": "test-events", "events": [a, b],
            "sources": sources, "replacement_edges": [], "settlement_receipts": [receipt],
            "arc_event_links": [{"arc_key": "arc", "event_id": "event_a", "linked_at": "old"}],
            "excluded_item_ids": ["fact_one"]}


def test_full_source_coverage_keeps_state_and_deduplicates_scene_snapshots(tmp_path, event_snapshot):
    with Store(tmp_path / "test.db") as store:
        store.create("scene_one", "scene", "Scene", "body")
        primary = store.add_source(encode(["assistant_bridge", "1", "1"]), "primary words", metadata={"from": "scene"})
        store.bind("scene_one", primary, record_action=False)
        report = import_events(store, event_snapshot)
        assert report["inserted_events"] == 2
        assert store.surface_state("event_a")["can_surface"]
        assert store.read("event_b")["lifecycle"] == "archived"
        assert store.read("event_b")["manual_surface"] is None
        assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 2
        supporting = store.add_source(encode(["assistant_bridge", "1", "2"]), "supporting words")
        store.bind("scene_one", supporting, record_action=False)
        before = store.read("event_a")
        changes = store.conn.total_changes
        assert store.surface_state("event_a")["reasons"] == ["covered_by_scene"]
        assert store.read("event_a") == before and store.conn.total_changes == changes
        assert store.conn.execute("SELECT result_json FROM event_settlement_receipts").fetchone()[0] == event_snapshot["settlement_receipts"][0]["result_json"]
        repeat = import_events(store, event_snapshot)
        assert repeat["inserted_events"] == repeat["inserted_bindings"] == 0
        assert not repeat["settlement_replayed"] and not repeat["bridge_cursor_modified"]


def test_active_predecessor_does_not_surface_and_missing_links_are_reported(tmp_path, event_snapshot):
    event_snapshot["replacement_edges"] = [{"predecessor_id": "event_a", "successor_id": "event_b", "created_at": "old"}]
    with Store(tmp_path / "test.db") as store:
        store.create("narrative_one", "narrative", "Narrative", "body")
        store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_one',1,'registry','event','event_missing','linked','{}')")
        report = import_events(store, event_snapshot)
        assert store.surface_state("event_a")["reasons"] == ["replaced_by_event"]
        assert store.read("event_a")["lifecycle"] == "active"
        assert report["active_replacement_predecessors"] == 1
        assert report["narrative_event_resolutions"] == {"missing": 1}
        assert store.read("event_missing") is None


def test_receipt_conflict_rolls_back_event_evidence_and_replacements(tmp_path, event_snapshot):
    with Store(tmp_path / "test.db") as store:
        store.conn.execute("INSERT INTO event_settlement_receipts VALUES ('operation_one','other','{}','old','other')")
        with pytest.raises(Conflict, match="receipt"):
            import_events(store, event_snapshot)
        for table in ("documents", "sources", "evidence_bindings", "import_records", "event_arc_links"):
            assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_bad_hash_is_rejected_without_partial_events(tmp_path, event_snapshot):
    event_snapshot["sources"][1]["content"] = "changed snapshot"
    with Store(tmp_path / "test.db") as store:
        with pytest.raises(ValueError, match="evidence hash"):
            import_events(store, event_snapshot)
        assert store.conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0


def test_fact_rows_cannot_be_imported_and_tombstones_stay_deleted(tmp_path, event_snapshot):
    bad = deepcopy(event_snapshot)
    bad["events"][0]["item_type"] = "fact"
    with Store(tmp_path / "test.db") as store:
        with pytest.raises(ValueError, match="Only Event"):
            import_events(store, bad)
        event_snapshot["events"][0]["status"] = "tombstoned"
        import_events(store, event_snapshot)
        assert store.read("event_a")["lifecycle"] == "deleted"
        assert store.conn.execute("SELECT 1 FROM deletions WHERE document_id='event_a'").fetchone()
        assert not store.surface_state("event_a")["can_surface"]


def test_export_filters_fact_data_and_keeps_event_receipt_bytes(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE fact_events (item_id TEXT, item_type TEXT)")
        db.execute("CREATE TABLE fact_event_sources (id INTEGER, item_id TEXT)")
        db.execute("CREATE TABLE fact_event_replacement_edges (predecessor_id TEXT, successor_id TEXT)")
        db.execute("CREATE TABLE fact_event_arc_links (arc_key TEXT, event_id TEXT)")
        db.execute("CREATE TABLE fact_event_settlement_operations (operation_id TEXT, result_json TEXT)")
        db.executemany("INSERT INTO fact_events VALUES (?,?)", [("event_a", "event"), ("fact_a", "fact")])
        db.executemany("INSERT INTO fact_event_sources VALUES (?,?)", [(1,"event_a"),(2,"fact_a")])
        db.executemany("INSERT INTO fact_event_settlement_operations VALUES (?,?)",
                       [("event_receipt", '{ "items": [{"item_id":"event_a"}] }'),
                        ("fact_receipt", '{"items":[{"item_id":"fact_a"}]}')])
    before = path.read_bytes()
    result = export_events(path)
    assert result["events"] == [{"item_id": "event_a", "item_type": "event"}]
    assert result["sources"] == [{"id": 1, "item_id": "event_a"}]
    assert result["excluded_receipt_ids"] == ["fact_receipt"]
    assert result["settlement_receipts"][0]["result_json"] == '{ "items": [{"item_id":"event_a"}] }'
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        export_events(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()

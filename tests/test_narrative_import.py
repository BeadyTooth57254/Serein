import base64
from copy import deepcopy
import json
import sqlite3

import pytest

from serein.core import Store, Conflict
from serein.core.store import digest, encode
from serein.ingest.legacy_narrative import import_narratives
from serein.ingest.narrative_snapshot import export_narratives


def artifact(path, value):
    raw = value if isinstance(value, bytes) else encode(value).encode("utf-8")
    return {"path": path, "bytes_b64": base64.b64encode(raw).decode(), "sha256": digest(raw)}


@pytest.fixture
def narrative_snapshot():
    old = b"# Original title\r\n\r\nold prose scene_deleted\r\n"
    current = b"# Current title\n\nbody event_unbound\n`docs/evidence/missing.jpg`\n"
    upload = b"\x00\xfforiginal bytes\r\n"
    roll = {"narrative_id": "narrative_one", "revision": 2, "title": "Current title",
            "source_file": "revisions/narrative_one/2.md", "document_sha256": digest(current),
            "lifecycle": "active", "publication_status": "collecting", "arc_key": "project:test",
            "linked_scene_ids": ["scene_one"], "excluded_scene_ids": ["scene_deleted"],
            "linked_upload_ids": ["upload_one"],
            "history": [{"revision": 1, "source_file": "docs/old.md", "document_sha256": digest(old),
                         "publication_status": "reviewed", "lifecycle": "active"}]}
    files = [artifact("registry.json", {"schema_version": "narrative-roll-registry-v1", "rolls": [roll]}),
             artifact("uploads/index.json", {"schema_version": "narrative-upload-v1", "items": [
                 {"upload_id": "upload_one", "blob_name": "one.bin", "sha256": digest(upload),
                  "size": len(upload), "filename": "original.dat", "extracted_text": "only a view"}]}),
             artifact("revision_inbox.json", {"schema_version": "narrative-revision-inbox-v1", "items": [
                 {"proposal_id": "proposal_one", "narrative_id": "narrative_one", "status": "pending",
                  "draft_delta": "Unapproved prose"}], "scan": {"last_scan_at": "original"}}),
             artifact("docs/old.md", old), artifact(roll["source_file"], current),
             artifact("revisions/narrative_one/unregistered.md", b"Not a published revision"),
             artifact("uploads/blobs/one.bin", upload)]
    return {"format": "serein-narrative-snapshot-v1", "origin": "test", "files": files,
            "arc_event_links": [{"arc_key": "project:test", "event_id": "event_appended", "linked_at": "original"}]}


def test_revisions_material_authority_and_original_upload_survive(tmp_path, narrative_snapshot):
    with Store(tmp_path / "test.db") as store:
        store.create("scene_one", "scene", "Scene", "scene body")
        store.record_deletion("scene_deleted", "old", {})
        report = import_narratives(store, narrative_snapshot)
        assert report["narratives"] == 1 and report["historical_revisions"] == 1
        assert store.read("narrative_one", revision=1)["body_md"].endswith("\r\n")
        assert store.read("narrative_one", revision=1)["title"] == "Original title"
        assert store.read("narrative_one")["metadata"]["legacy_registry"]["publication_status"] == "collecting"
        assert not store.surface_state("narrative_one")["can_surface"]
        assert store.conn.execute("SELECT status FROM narrative_proposals").fetchone()[0] == "pending"
        assert store.conn.execute("SELECT count(*) FROM revisions WHERE document_id='narrative_one'").fetchone()[0] == 2
        assert report["unregistered_documents"] == ["revisions/narrative_one/unregistered.md"]
        assert report["local_file_references"][0]["available"] is False
        refs = {r["target_id"]: r for r in report["material_checks"] if r["revision"] == 2}
        assert refs["scene_deleted"]["disposition"] == "excluded"
        assert refs["scene_deleted"]["resolution"] == "deleted"
        assert refs["event_unbound"]["disposition"] == "mentioned"
        assert refs["event_appended"]["disposition"] == "appended"
        assert refs["event_appended"]["resolution"] == "pending_adapter"
        assert store.read("event_appended") is None
        raw = store.conn.execute("SELECT r.content FROM narrative_uploads u JOIN import_records r "
                                 "ON r.origin=u.origin AND r.path=u.path WHERE u.id='upload_one'").fetchone()[0]
        assert raw == b"\x00\xfforiginal bytes\r\n"
        before = store.conn.execute("SELECT count(*) FROM narrative_materials").fetchone()[0]
        assert import_narratives(store, narrative_snapshot)["inserted_narratives"] == 0
        assert store.conn.execute("SELECT count(*) FROM narrative_materials").fetchone()[0] == before


def test_late_proposal_conflict_rolls_back_all_narratives_and_uploads(tmp_path, narrative_snapshot):
    with Store(tmp_path / "test.db") as store:
        store.conn.execute("INSERT INTO narrative_proposals VALUES ('proposal_one','other','','dismissed','{}')")
        with pytest.raises(Conflict, match="proposal"):
            import_narratives(store, narrative_snapshot)
        for table in ("documents", "revisions", "import_records", "narrative_uploads", "narrative_materials"):
            assert store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_registry_hash_mismatch_does_not_publish_a_different_document(tmp_path, narrative_snapshot):
    narrative_snapshot["files"][4] = artifact("revisions/narrative_one/2.md", b"A changed file")
    with Store(tmp_path / "test.db") as store:
        with pytest.raises(ValueError, match="Registry document checksum"):
            import_narratives(store, narrative_snapshot)
        assert store.read("narrative_one") is None
        assert store.conn.execute("SELECT count(*) FROM import_records").fetchone()[0] == 0


def test_changed_snapshot_never_overwrites_existing_narrative(tmp_path, narrative_snapshot):
    with Store(tmp_path / "test.db") as store:
        import_narratives(store, narrative_snapshot)
        original = store.read("narrative_one")
        changed = deepcopy(narrative_snapshot)
        changed["origin"] = "different-run"
        with pytest.raises(Conflict):
            import_narratives(store, changed)
        assert store.read("narrative_one") == original


def test_export_reads_without_writes_and_filters_fact_links(tmp_path, narrative_snapshot):
    root = tmp_path / "narrative_rolls"
    for entry in narrative_snapshot["files"]:
        target = root / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(entry["bytes_b64"]))
    with sqlite3.connect(tmp_path / "fact_events.sqlite") as db:
        db.execute("CREATE TABLE fact_events (item_id TEXT, item_type TEXT)")
        db.execute("CREATE TABLE fact_event_arc_links (arc_key TEXT, event_id TEXT)")
        db.executemany("INSERT INTO fact_events VALUES (?,?)", [("event_one", "event"), ("fact_one", "fact")])
        db.executemany("INSERT INTO fact_event_arc_links VALUES ('arc',?)", [("event_one",), ("fact_one",)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    exported = export_narratives(tmp_path)
    assert len(exported["files"]) == len(narrative_snapshot["files"])
    assert exported["arc_event_links"] == [{"arc_key": "arc", "event_id": "event_one"}]
    assert all(path.read_bytes() == content for path, content in before.items())

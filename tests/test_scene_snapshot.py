from pathlib import Path
import sqlite3

import pytest

from serein.ingest.scene_snapshot import export_snapshot, write_snapshot


def test_export_is_read_only_and_excludes_unidentified_buckets(tmp_path):
    buckets, state = tmp_path / "buckets", tmp_path / "state"
    (buckets / "dynamic").mkdir(parents=True)
    state.mkdir()
    (buckets / "dynamic/scene.md").write_text(
        "---\nid: scene_one\nwrite_contract: write-scene-v1\n---\nbody", encoding="utf-8")
    (buckets / "dynamic/legacy.md").write_text("---\nid: old_bucket\n---\nlegacy", encoding="utf-8")
    for name, tables in [("scene_evidence.sqlite", ["scene_evidence", "scene_evidence_events"]),
                         ("scene_edge_proposals.sqlite", ["scene_edges", "scene_edge_proposals"])]:
        with sqlite3.connect(state / name) as db:
            for table in tables:
                db.execute(f"CREATE TABLE {table} (id INTEGER)")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = export_snapshot(buckets, state)
    assert len(result["scenes"]) == 1
    assert result["excluded_documents"][0]["id"] == "old_bucket"
    assert all(path.read_bytes() == content for path, content in before.items())
    output = tmp_path / "output.json"
    write_snapshot(result, output)
    with pytest.raises(FileExistsError):
        write_snapshot(result, output)


def test_missing_input_does_not_create_legacy_database(tmp_path):
    buckets, state = tmp_path / "buckets", tmp_path / "state"
    buckets.mkdir()
    state.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        export_snapshot(buckets, state)
    assert not list(state.iterdir())

import sqlite3

import pytest

from serein.adapters.bridge import BridgeMessages
from serein.application import Application
from serein.config import Settings
from serein.core import Store
from serein.core.search import build_index


def test_only_selected_sessions_supply_verbatim_evidence(tmp_path):
    source_db, target_db = tmp_path / "bridge.db", tmp_path / "serein.db"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id INTEGER,role TEXT,content TEXT,created_at TEXT)")
        conn.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [(1, 7, "user", "原文\r\nexact", "time"),
                                                                 (2, 99, "assistant", "unrelated work", "time"),
                                                                 (3, 7, "system", "instructions", "time")])
    source = BridgeMessages(source_db, [7], "bridge")
    assert [row["id"] for row in source.list()["items"]] == [1]
    assert source.list()["cursor_persisted"] is False
    for ids in ([2], [3], [1, 2]):
        with pytest.raises(ValueError, match="scope"):
            source.read(ids)
    with Store(target_db):
        pass
    services = Application(Settings(target_db, writable=True, source={"database": source_db, "session_ids": [7], "system": "bridge"})).services
    result = services.write("verified-source", "save", {"kind": "event", "title": "记忆", "body_md": "有证据的经历",
                                                        "source_message_ids": [1]})
    assert services.read(result["id"])["evidence"][0]["content"] == "原文\r\nexact"
    with sqlite3.connect(source_db) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        conn.execute("UPDATE messages SET content='edited later' WHERE id=1")
    again = services.write("verified-source", "save", {"kind": "event", "title": "记忆", "body_md": "有证据的经历",
                                                       "source_message_ids": [1]})
    assert again["id"] == result["id"]
    assert services.read(result["id"])["evidence"][0]["content"] == "原文\r\nexact"
    with pytest.raises(ValueError):
        BridgeMessages(source_db, [], "bridge")


def test_bridge_source_to_frozen_candidate_acceptance_and_recall(tmp_path):
    source_db, target_db, index = tmp_path / "assistant.db", tmp_path / "serein.db", tmp_path / "recall.sqlite"
    with sqlite3.connect(source_db) as conn:
        conn.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id INTEGER,role TEXT,content TEXT,created_at TEXT)")
        conn.execute("INSERT INTO messages VALUES (1,7,'user','今天雨天归航，原话。','2026-09-06')")
        conn.execute("INSERT INTO messages VALUES (2,8,'assistant','打工会话','2026-09-06')")
    with Store(target_db):
        pass
    build_index(target_db, index)
    services = Application(Settings(target_db, index, writable=True,
                                     source={"database": source_db, "session_ids": [7], "system": "assistant_bridge"})).services
    proposal = services.write("track-unit-1-proposal", "propose", {
        "kind": "event", "title": "雨天归航", "body_md": "一起记住归航的这一天。", "source_message_ids": [1]})
    assert not services.search("归航")["items"]
    with sqlite3.connect(source_db) as conn:
        conn.execute("UPDATE messages SET content='后来修改的消息' WHERE id=1")
    accepted = services.write("track-unit-1-accept", "review", {"candidate_id": proposal["id"], "decision": "accept"})
    saved_id = accepted["document"]["id"]
    evidence = services.read(saved_id)["evidence"]
    assert len(evidence) == 1 and evidence[0]["content"] == "今天雨天归航，原话。"
    assert services.recall("归航", mode="lookup")["pools"]["event"]["items"][0]["id"] == saved_id
    assert not services.candidates()["items"]
    assert services.write("track-unit-1-accept", "review", {"candidate_id": proposal["id"], "decision": "accept"})["document"]["id"] == saved_id

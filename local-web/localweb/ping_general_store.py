"""Durable command boundaries and completion cursors for finite general Ping runs."""

import json
import sqlite3
from contextlib import closing

from .ping_jobs import PingBatch
from .ping_tools import PingPlan


def prepare(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS general_ping_runs ("
        "id TEXT PRIMARY KEY, plan TEXT NOT NULL, pair_indices TEXT NOT NULL, result TEXT)"
    )
    columns = {col[1] for col in db.execute("PRAGMA table_info(general_ping_runs)")}
    if "journal_version" not in columns:
        db.execute("ALTER TABLE general_ping_runs ADD journal_version INTEGER NOT NULL DEFAULT 0")
    if "parent_id" not in columns:
        db.execute("ALTER TABLE general_ping_runs ADD parent_id TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS general_ping_slots ("
        "run_id TEXT NOT NULL, pair_index INTEGER NOT NULL, started TEXT, "
        "stream_index INTEGER, row_json TEXT, PRIMARY KEY (run_id,pair_index))"
    )


def before_send(path, run_id, slot):
    # A crash after this commit and before the SSH write still counts as uncertain.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO general_ping_slots(run_id,pair_index,started) VALUES(?,?,?)",
            (run_id, slot.index, slot.started),
        )


def record(path, run_id, row):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "INSERT INTO general_ping_slots(run_id,pair_index,stream_index,row_json) "
            "VALUES(?,?,?,?) ON CONFLICT(run_id,pair_index) DO UPDATE SET "
            "stream_index=excluded.stream_index,row_json=excluded.row_json",
            (run_id, row["pairIndex"], row["index"], json.dumps(row, ensure_ascii=False)),
        )


def finalize(db, key, snapshot):
    db.execute(
        "UPDATE general_ping_runs SET result=? WHERE id=?",
        (json.dumps(snapshot, ensure_ascii=False), key),
    )
    db.execute("DELETE FROM general_ping_slots WHERE run_id=?", (key,))


def recover_interrupted(db):
    # Legacy runs without write-ahead evidence must stay history-only.
    for key, plan, indices, parent in db.execute(
        "SELECT p.id,p.plan,p.pair_indices,p.parent_id FROM general_ping_runs p "
        "JOIN jobs j ON j.id=p.id WHERE p.journal_version=1 AND p.result IS NULL "
        "AND j.state='interrupted'"
    ).fetchall():
        batch = PingBatch(key, PingPlan.parse(json.loads(plan)), None, json.loads(indices), parent)
        batch.stop("SERVER_RESTART", state="interrupted")
        evidence = db.execute(
            "SELECT pair_index,started,row_json FROM general_ping_slots WHERE run_id=? "
            "ORDER BY (stream_index IS NULL),stream_index,pair_index",
            (key,),
        )
        for pair_index, started, recorded in evidence:
            if recorded is not None:
                batch.rows.append(json.loads(recorded))
            else:
                batch.record_interrupted(pair_index, started)
        snapshot = batch.snapshot(complete=True)
        finalize(db, key, snapshot)
        db.execute(
            "UPDATE jobs SET summary=? WHERE id=?",
            (f"Ping 중단 · {snapshot['processedPairs']}/{snapshot['total']}", key),
        )

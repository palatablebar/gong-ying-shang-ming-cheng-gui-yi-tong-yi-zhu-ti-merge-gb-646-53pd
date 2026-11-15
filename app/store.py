"""SQLite 存储：批次运行清单、采信结果、候选对、合并簇、复核决定。

金额一律以字符串（Decimal 原值）存取，不在数据库层做浮点运算。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH as DEFAULT_DB_PATH

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        input_sha256 TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        params_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        elapsed_ms INTEGER NOT NULL,
        counts_json TEXT NOT NULL,
        files_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS records (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        supplier_no TEXT NOT NULL,
        raw_name TEXT NOT NULL,
        norm_name TEXT NOT NULL DEFAULT '',
        norm_codes TEXT NOT NULL DEFAULT '[]',
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        bank_account TEXT NOT NULL DEFAULT '',
        erp TEXT NOT NULL DEFAULT '',
        ref_master_id TEXT NOT NULL DEFAULT '',
        ytd_amount TEXT NOT NULL DEFAULT '0.00',
        flags TEXT NOT NULL DEFAULT '[]',
        source_file TEXT NOT NULL DEFAULT '',
        line_no INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS quarantine (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL,
        raw_line TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS conflicts (
        run_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        field TEXT NOT NULL,
        kept TEXT NOT NULL,
        rejected TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line_no INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS blocks (
        run_id TEXT NOT NULL,
        block_key TEXT NOT NULL,
        member_count INTEGER NOT NULL
    )""",
    # 分阶段完成标记：保证"关系承接 → 候选对 → 簇"各算一次，重跑不重复记账。
    """CREATE TABLE IF NOT EXISTS stage_marks (
        run_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        signature TEXT NOT NULL,
        stamped_at TEXT NOT NULL,
        PRIMARY KEY (run_id, stage)
    )""",
    """CREATE TABLE IF NOT EXISTS block_pairs (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        shared_keys TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS pair_scores (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        dice TEXT NOT NULL,
        ratio TEXT NOT NULL,
        score TEXT NOT NULL,
        verdict TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',
        carry_status TEXT NOT NULL DEFAULT '',
        carry_code TEXT NOT NULL DEFAULT '',
        carry_detail TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, pair_id)
    )""",
    """CREATE TABLE IF NOT EXISTS clusters (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        rep_id TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL,
        total_amount TEXT NOT NULL,
        conflict INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (run_id, cluster_id)
    )""",
    """CREATE TABLE IF NOT EXISTS cluster_members (
        run_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        share_amount TEXT NOT NULL,
        PRIMARY KEY (run_id, cluster_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS decisions (
        run_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        expected_version INTEGER NOT NULL,
        version INTEGER NOT NULL,
        operator TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT 'manual',
        source_version TEXT NOT NULL DEFAULT '',
        carry_code TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, pair_id)
    )""",
    # 跨批次承接：按业务身份键记录上一版确认关系在新批次的去向。
    """CREATE TABLE IF NOT EXISTS carry_runs (
        run_id TEXT PRIMARY KEY,
        previous_version TEXT NOT NULL DEFAULT '',
        computed_at TEXT NOT NULL,
        counts_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS carry_entries (
        run_id TEXT NOT NULL,
        relation_key TEXT NOT NULL,
        source_version TEXT NOT NULL DEFAULT '',
        prev_left_key TEXT NOT NULL,
        prev_right_key TEXT NOT NULL,
        left_key TEXT NOT NULL,
        right_key TEXT NOT NULL,
        left_id TEXT NOT NULL DEFAULT '',
        right_id TEXT NOT NULL DEFAULT '',
        pair_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        score TEXT NOT NULL DEFAULT '',
        reason_code TEXT NOT NULL DEFAULT '',
        prev_decision TEXT NOT NULL DEFAULT '',
        operator TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (run_id, relation_key)
    )""",
    # 版本：正式主数据与复核决定一次性发布，按内容摘要去重。
    """CREATE TABLE IF NOT EXISTS versions (
        version_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        content_digest TEXT NOT NULL UNIQUE,
        parent_version TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        published_at TEXT NOT NULL,
        counts_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS version_clusters (
        version_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        rep_id TEXT NOT NULL,
        rep_key TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        member_count INTEGER NOT NULL,
        total_amount TEXT NOT NULL,
        conflict INTEGER NOT NULL DEFAULT 0,
        member_keys TEXT NOT NULL DEFAULT '[]',
        previous_cluster_id TEXT NOT NULL DEFAULT '',
        subject_status TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (version_id, cluster_id)
    )""",
    """CREATE TABLE IF NOT EXISTS version_members (
        version_id TEXT NOT NULL,
        cluster_id TEXT NOT NULL,
        supplier_id TEXT NOT NULL,
        identity_key TEXT NOT NULL,
        tax_id TEXT NOT NULL DEFAULT '',
        norm_name TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        share_amount TEXT NOT NULL,
        PRIMARY KEY (version_id, cluster_id, supplier_id)
    )""",
    """CREATE TABLE IF NOT EXISTS version_decisions (
        version_id TEXT NOT NULL,
        pair_id TEXT NOT NULL,
        left_key TEXT NOT NULL,
        right_key TEXT NOT NULL,
        left_id TEXT NOT NULL,
        right_id TEXT NOT NULL,
        decision TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        score TEXT NOT NULL DEFAULT '',
        operator TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL DEFAULT '',
        row_version INTEGER NOT NULL DEFAULT 0,
        origin TEXT NOT NULL DEFAULT 'manual',
        source_version TEXT NOT NULL DEFAULT '',
        carry_code TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (version_id, pair_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_pairs_run ON pair_scores(run_id)",
)

TABLES = ("runs", "records", "quarantine", "conflicts", "blocks", "block_pairs",
          "pair_scores", "clusters", "cluster_members", "decisions",
          "carry_runs", "carry_entries", "stage_marks",
          "versions", "version_clusters", "version_members", "version_decisions")


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    """按环境变量解析，便于测试指向临时库。"""
    return Path(os.environ.get("MERGE_DB", str(DEFAULT_DB_PATH)))


def connect(path=None) -> sqlite3.Connection:
    target = Path(path or db_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 增量列：老库升级时补列，避免 CREATE TABLE IF NOT EXISTS 留下旧结构。
COLUMN_MIGRATIONS = {
    "pair_scores": (
        ("carry_status", "TEXT NOT NULL DEFAULT ''"),
        ("carry_code", "TEXT NOT NULL DEFAULT ''"),
        ("carry_detail", "TEXT NOT NULL DEFAULT ''"),
    ),
    "decisions": (
        ("origin", "TEXT NOT NULL DEFAULT 'manual'"),
        ("source_version", "TEXT NOT NULL DEFAULT ''"),
        ("carry_code", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _migrate(conn) -> None:
    for table, columns in COLUMN_MIGRATIONS.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue  # 表尚未创建，交给后续 CREATE 语句
        for name, definition in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db(path=None) -> None:
    with connect(path) as conn:
        for statement in SCHEMA:
            conn.execute(statement)
        _migrate(conn)
        conn.commit()


def reset_db(path=None) -> None:
    with connect(path) as conn:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ---- runs ----

def save_run(run: dict) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run["run_id"],))
        conn.execute(
            "INSERT INTO runs (run_id, input_sha256, rule_version, params_json, created_at,"
            " elapsed_ms, counts_json, files_json) VALUES (?,?,?,?,?,?,?,?)",
            (run["run_id"], run["input_sha256"], run["rule_version"], run["params_json"],
             run["created_at"], run["elapsed_ms"], run["counts_json"], run["files_json"]),
        )
        conn.commit()


def get_run(run_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs WHERE run_id = ?", (run_id,))


def latest_run():
    # 同一秒内采信两批时按插入顺序判定"最新"，不再依赖批次号字典序。
    with connect() as conn:
        return _one(conn, "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC LIMIT 1")


def list_runs() -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM runs ORDER BY created_at DESC, rowid DESC")


def update_run_counts(run_id: str, counts: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET counts_json = ? WHERE run_id = ?",
                     (json.dumps(counts, ensure_ascii=False, sort_keys=True), run_id))
        conn.commit()


# ---- 采信结果 ----

def clear_stage(run_id: str, stage: str) -> None:
    tables = {
        "intake": ("records", "quarantine", "conflicts"),
        "normalize": (),
        "blocks": ("blocks", "block_pairs"),
        "pairs": ("pair_scores",),
        "clusters": ("clusters", "cluster_members"),
    }[stage]
    with connect() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        conn.commit()


def save_records(run_id: str, records) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO records (run_id, supplier_id, supplier_no, raw_name,"
            " norm_name, norm_codes, tax_id, city, bank_account, erp, ref_master_id, ytd_amount,"
            " flags, source_file, line_no) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["supplier_no"], r["raw_name"], r.get("norm_name", ""),
              r.get("norm_codes", "[]"), r["tax_id"], r["city"], r["bank_account"], r["erp"],
              r["ref_master_id"], str(r["ytd_amount"]), r.get("flags", "[]"),
              r["source_file"], r["line_no"]) for r in records],
        )
        conn.commit()


def list_records(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM records WHERE run_id = ? ORDER BY supplier_id", (run_id,))


def get_record(run_id: str, supplier_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM records WHERE run_id = ? AND supplier_id = ?",
                    (run_id, supplier_id))


def update_norm(run_id: str, supplier_id: str, norm_name: str, norm_codes: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE records SET norm_name = ?, norm_codes = ?"
                     " WHERE run_id = ? AND supplier_id = ?",
                     (norm_name, norm_codes, run_id, supplier_id))
        conn.commit()


def save_quarantine(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO quarantine (run_id, seq, source_file, line_no, raw_line, reason_code,"
            " detail) VALUES (?,?,?,?,?,?,?)",
            [(run_id, i, r["source_file"], r["line_no"], r["raw_line"], r["reason_code"],
              r.get("detail", "")) for i, r in enumerate(rows, 1)],
        )
        conn.commit()


def list_quarantine(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM quarantine WHERE run_id = ? ORDER BY seq", (run_id,))


def save_conflicts(run_id: str, rows) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO conflicts (run_id, supplier_id, field, kept, rejected, reason_code,"
            " source_file, line_no) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, r["supplier_id"], r["field"], r["kept"], r["rejected"], r["reason_code"],
              r["source_file"], r["line_no"]) for r in rows],
        )
        conn.commit()


def list_conflicts(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM conflicts WHERE run_id = ? ORDER BY supplier_id", (run_id,))


# ---- 分块 ----

def save_blocks(run_id: str, blocks) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM blocks WHERE run_id = ?", (run_id,))
        conn.executemany("INSERT INTO blocks (run_id, block_key, member_count) VALUES (?,?,?)",
                         [(run_id, key, count) for key, count in blocks])
        conn.commit()


def list_blocks(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM blocks WHERE run_id = ? ORDER BY member_count DESC,"
                           " block_key", (run_id,))


def save_block_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM block_pairs WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO block_pairs (run_id, pair_id, left_id, right_id, shared_keys)"
            " VALUES (?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], p["shared_keys"]) for p in pairs],
        )
        conn.commit()


def list_block_pairs(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM block_pairs WHERE run_id = ? ORDER BY pair_id", (run_id,))


# ---- 候选对 ----

def save_pairs(run_id: str, pairs) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pair_scores WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO pair_scores (run_id, pair_id, left_id, right_id, dice, ratio, score,"
            " verdict, reason_code, detail, carry_status, carry_code, carry_detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, p["pair_id"], p["left_id"], p["right_id"], str(p["dice"]), str(p["ratio"]),
              str(p["score"]), p["verdict"], p["reason_code"], p.get("detail", ""),
              p.get("carry_status", ""), p.get("carry_code", ""), p.get("carry_detail", ""))
             for p in pairs],
        )
        conn.commit()


def update_pair_carry(run_id: str, updates) -> None:
    """承接判定覆盖候选对的分档与引用，逐条带原分档以便回溯。"""
    with connect() as conn:
        conn.executemany(
            "UPDATE pair_scores SET verdict = ?, carry_status = ?, carry_code = ?,"
            " carry_detail = ? WHERE run_id = ? AND pair_id = ?",
            [(u["verdict"], u["carry_status"], u["carry_code"], u["carry_detail"], run_id,
              u["pair_id"]) for u in updates],
        )
        conn.commit()


def list_pairs(run_id: str, verdict: str = "") -> list:
    with connect() as conn:
        if verdict:
            return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND verdict = ?"
                               " ORDER BY score DESC, pair_id", (run_id, verdict))
        return _rows(conn, "SELECT * FROM pair_scores WHERE run_id = ?"
                           " ORDER BY score DESC, pair_id", (run_id,))


def get_pair(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM pair_scores WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


def upsert_pair(run_id: str, pair: dict) -> None:
    """承接关系在当前批次没有自然候选对时补一条，保证旧结论仍能生效。"""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pair_scores (run_id, pair_id, left_id, right_id, dice, ratio,"
            " score, verdict, reason_code, detail, carry_status, carry_code, carry_detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, pair["pair_id"], pair["left_id"], pair["right_id"], str(pair["dice"]),
             str(pair["ratio"]), str(pair["score"]), pair["verdict"], pair["reason_code"],
             pair.get("detail", ""), pair.get("carry_status", ""), pair.get("carry_code", ""),
             pair.get("carry_detail", "")),
        )
        conn.commit()


# ---- 合并簇 ----

def save_clusters(run_id: str, clusters, members) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM clusters WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM cluster_members WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO clusters (run_id, cluster_id, rep_id, tax_id, city, member_count,"
            " total_amount, conflict) VALUES (?,?,?,?,?,?,?,?)",
            [(run_id, c["cluster_id"], c["rep_id"], c["tax_id"], c["city"], c["member_count"],
              str(c["total_amount"]), 1 if c["conflict"] else 0) for c in clusters],
        )
        conn.executemany(
            "INSERT INTO cluster_members (run_id, cluster_id, supplier_id, share_amount)"
            " VALUES (?,?,?,?)",
            [(run_id, m["cluster_id"], m["supplier_id"], str(m["share_amount"])) for m in members],
        )
        conn.commit()


def list_clusters(run_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM clusters WHERE run_id = ?"
                           " ORDER BY member_count DESC, cluster_id", (run_id,))


def list_cluster_members(run_id: str, cluster_id: str = "") -> list:
    with connect() as conn:
        if cluster_id:
            return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ? AND cluster_id = ?"
                               " ORDER BY supplier_id", (run_id, cluster_id))
        return _rows(conn, "SELECT * FROM cluster_members WHERE run_id = ?"
                           " ORDER BY cluster_id, supplier_id", (run_id,))


# ---- 复核 ----

def get_decision(run_id: str, pair_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM decisions WHERE run_id = ? AND pair_id = ?",
                    (run_id, pair_id))


def list_decisions(run_id: str, since: str = "", until: str = "") -> list:
    with connect() as conn:
        sql = "SELECT * FROM decisions WHERE run_id = ?"
        params = [run_id]
        if since:
            sql += " AND decided_at >= ?"
            params.append(since)
        if until:
            sql += " AND decided_at < ?"
            params.append(until)
        sql += " ORDER BY decided_at DESC, pair_id"
        return _rows(conn, sql, tuple(params))


def save_decision(decision: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO decisions (run_id, pair_id, decision, expected_version,"
            " version, operator, decided_at, origin, source_version, carry_code)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (decision["run_id"], decision["pair_id"], decision["decision"],
             decision["expected_version"], decision["version"], decision["operator"],
             decision["decided_at"], decision.get("origin", "manual"),
             decision.get("source_version", ""), decision.get("carry_code", "")),
        )
        conn.commit()


# ---- 分阶段标记 ----

def stage_signature(run_id: str, stage: str):
    with connect() as conn:
        row = _one(conn, "SELECT * FROM stage_marks WHERE run_id = ? AND stage = ?",
                   (run_id, stage))
    return row["signature"] if row else None


def mark_stage(run_id: str, stage: str, signature: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO stage_marks (run_id, stage, signature, stamped_at)"
                     " VALUES (?,?,?,?)", (run_id, stage, signature, utc_iso_now()))
        conn.commit()


def clear_stages(run_id: str, stages) -> None:
    with connect() as conn:
        conn.executemany("DELETE FROM stage_marks WHERE run_id = ? AND stage = ?",
                         [(run_id, stage) for stage in stages])
        conn.commit()


# ---- 跨批次承接 ----

def save_carry_meta(run_id: str, previous_version: str, computed_at: str, counts: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO carry_runs (run_id, previous_version, computed_at, counts_json)"
            " VALUES (?,?,?,?)",
            (run_id, previous_version, computed_at,
             json.dumps(counts, ensure_ascii=False, sort_keys=True)),
        )
        conn.commit()


def save_carry_entries(run_id: str, kind: str, entries) -> None:
    """按 kind 整批替换，重跑同一批不重复记账。"""
    with connect() as conn:
        conn.execute("DELETE FROM carry_entries WHERE run_id = ? AND kind = ?", (run_id, kind))
        conn.executemany(
            "INSERT OR REPLACE INTO carry_entries (run_id, relation_key, source_version,"
            " prev_left_key, prev_right_key, left_key, right_key, left_id, right_id, pair_id, kind,"
            " status, code, detail, score, reason_code, prev_decision, operator, decided_at,"
            " evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, e["relation_key"], e.get("source_version", ""), e["prev_left_key"],
              e["prev_right_key"], e["left_key"], e["right_key"], e.get("left_id", ""),
              e.get("right_id", ""), e.get("pair_id", ""), kind, e["status"],
              e.get("code", ""), e.get("detail", ""), str(e.get("score", "")),
              e.get("reason_code", ""), e.get("prev_decision", ""), e.get("operator", ""),
              e.get("decided_at", ""),
              json.dumps(e.get("evidence", {}), ensure_ascii=False, sort_keys=True))
             for e in entries],
        )
        conn.commit()


def get_carry_run(run_id: str):
    with connect() as conn:
        row = _one(conn, "SELECT * FROM carry_runs WHERE run_id = ?", (run_id,))
    if row:
        row["counts"] = json.loads(row.get("counts_json") or "{}")
    return row


def list_carry_entries(run_id: str) -> list:
    with connect() as conn:
        rows = _rows(conn, "SELECT * FROM carry_entries WHERE run_id = ?"
                           " ORDER BY kind, relation_key", (run_id,))
    for row in rows:
        row["evidence"] = json.loads(row.get("evidence_json") or "{}")
    return rows


def get_carry_entry(run_id: str, relation_key: str):
    with connect() as conn:
        row = _one(conn, "SELECT * FROM carry_entries WHERE run_id = ? AND relation_key = ?",
                   (run_id, relation_key))
    if row:
        row["evidence"] = json.loads(row.get("evidence_json") or "{}")
    return row


# ---- 版本发布 ----

def latest_version(status: str = "published"):
    with connect() as conn:
        if status:
            return _one(conn, "SELECT * FROM versions WHERE status = ?"
                              " ORDER BY seq DESC LIMIT 1", (status,))
        return _one(conn, "SELECT * FROM versions ORDER BY seq DESC LIMIT 1")


def list_versions(status: str = "", limit: int = 20) -> list:
    with connect() as conn:
        if status:
            return _rows(conn, "SELECT * FROM versions WHERE status = ?"
                               " ORDER BY seq DESC LIMIT ?", (status, limit))
        return _rows(conn, "SELECT * FROM versions ORDER BY seq DESC LIMIT ?", (limit,))


def get_version(version_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM versions WHERE version_id = ?", (version_id,))


def version_by_run(run_id: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM versions WHERE run_id = ? AND status = 'published'"
                          " ORDER BY seq DESC LIMIT 1", (run_id,))


def get_version_by_digest(digest: str):
    with connect() as conn:
        return _one(conn, "SELECT * FROM versions WHERE content_digest = ?", (digest,))


def version_clusters(version_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM version_clusters WHERE version_id = ?"
                           " ORDER BY cluster_id", (version_id,))


def version_members(version_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM version_members WHERE version_id = ?"
                           " ORDER BY cluster_id, supplier_id", (version_id,))


def version_decisions(version_id: str) -> list:
    with connect() as conn:
        return _rows(conn, "SELECT * FROM version_decisions WHERE version_id = ?"
                           " ORDER BY pair_id", (version_id,))


def publish_version(version: dict, clusters, members, decisions) -> None:
    """单一事务写入版本头、主数据与复核决定；任何一步失败整体回滚。"""
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM version_clusters WHERE version_id = ?", (version["version_id"],))
            conn.execute("DELETE FROM version_members WHERE version_id = ?", (version["version_id"],))
            conn.execute("DELETE FROM version_decisions WHERE version_id = ?", (version["version_id"],))
            conn.execute(
                "INSERT OR REPLACE INTO versions (version_id, seq, run_id, status, rule_version,"
                " content_digest, parent_version, created_at, published_at, counts_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (version["version_id"], version["seq"], version["run_id"], version["status"],
                 version["rule_version"], version["content_digest"], version.get("parent_version", ""),
                 version["created_at"], version["published_at"], version["counts_json"]),
            )
            conn.executemany(
                "INSERT INTO version_clusters (version_id, cluster_id, rep_id, rep_key, tax_id,"
                " city, member_count, total_amount, conflict, member_keys, previous_cluster_id,"
                " subject_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(version["version_id"], c["cluster_id"], c["rep_id"], c["rep_key"],
                  c["tax_id"], c["city"], c["member_count"], str(c["total_amount"]),
                  1 if c["conflict"] else 0, json.dumps(c["member_keys"], ensure_ascii=False),
                  c.get("previous_cluster_id", ""), c.get("subject_status", "")) for c in clusters],
            )
            conn.executemany(
                "INSERT INTO version_members (version_id, cluster_id, supplier_id, identity_key,"
                " tax_id, norm_name, city, share_amount) VALUES (?,?,?,?,?,?,?,?)",
                [(version["version_id"], m["cluster_id"], m["supplier_id"], m["identity_key"],
                  m["tax_id"], m["norm_name"], m["city"], str(m["share_amount"]))
                 for m in members],
            )
            conn.executemany(
                "INSERT INTO version_decisions (version_id, pair_id, left_key, right_key, left_id,"
                " right_id, decision, reason_code, score, operator, decided_at, row_version, origin,"
                " source_version, carry_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(version["version_id"], d["pair_id"], d["left_key"], d["right_key"], d["left_id"],
                  d["right_id"], d["decision"], d.get("reason_code", ""), str(d.get("score", "")),
                  d.get("operator", ""), d.get("decided_at", ""), int(d.get("row_version", 0)),
                  d.get("origin", "manual"), d.get("source_version", ""), d.get("carry_code", ""))
                 for d in decisions],
            )
    finally:
        conn.close()


def delete_version(version_id: str) -> None:
    """发布失败时清掉本次新建的版本，上一已发布版本保持原样。"""
    with connect() as conn:
        conn.execute("DELETE FROM version_clusters WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM version_members WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM version_decisions WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM versions WHERE version_id = ?", (version_id,))
        conn.commit()


def mark_version_status(version_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE versions SET status = ? WHERE version_id = ?", (status, version_id))
        conn.commit()


def delete_carried_decisions(run_id: str, pair_ids) -> None:
    """撤掉系统沿用的决定行（人工决定不受影响）。"""
    with connect() as conn:
        conn.executemany(
            "DELETE FROM decisions WHERE run_id = ? AND pair_id = ? AND origin = 'carried'",
            [(run_id, pair_id) for pair_id in pair_ids],
        )
        conn.commit()


def count(run_id: str, table: str) -> int:
    run_tables = ("records", "quarantine", "conflicts", "blocks", "block_pairs",
                  "pair_scores", "clusters", "cluster_members", "decisions",
                  "carry_entries")
    if table not in run_tables:
        raise ValueError(table)
    with connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["n"])

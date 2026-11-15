"""M1 名录采信：编码回退、BOM、列数、空值哨兵、前导零、冲突与空态。"""
from __future__ import annotations

from decimal import Decimal

from app import store
from app.config import SAMPLE_DIR
from app.modules.m1_intake import columns, decode, reader, service
from tests.fixtures import expected as EXP

PAYLOAD = ("supplier_id", "supplier_no", "raw_name", "tax_id", "city",
           "bank_account", "erp_source", "ref_master_id", "ytd_amount", "last_update")


def _file(name: str) -> bytes:
    return (SAMPLE_DIR / name).read_bytes()


def test_gb18030_file_decoded():
    """TRAP-01 编码混用：GB18030 文件必须能读出中文列与中文名称。"""
    decoded = decode.decode_bytes(_file("erp_c_north_gb18030.csv"))
    assert decoded.encoding == "gb18030"
    assert "太原晋阳煤化有限公司" in decoded.text
    assert decoded.bom_removed is False
    assert decoded.attempts == ("utf-8", "gb18030")


def test_bom_stripped_from_header():
    """TRAP-02 UTF-8 BOM：首列名不能带 \\ufeff。"""
    decoded = decode.decode_bytes(_file("erp_b_alias_bom.csv"))
    assert decoded.bom_removed is True
    parsed = reader.parse_stream(decoded.text)
    header = columns.normalize_header(parsed.header)
    assert header[0] == "supplier_id"
    assert "\ufeff" not in header[0]
    assert columns.map_columns(header)["name"] == 2


def test_short_row_quarantined(run):
    """TRAP-05 列数不齐：少列的行进隔离区并保留原始行号与原值。"""
    rows = store.list_quarantine(run["run_id"])
    short = [row for row in rows if row["reason_code"] == "Q_ROW_SHORT"]
    assert len(short) == 1
    assert short[0]["source_file"] == "erp_b_alias_bom.csv"
    assert short[0]["line_no"] == 21
    assert short[0]["detail"] == "3/10"
    assert "滇池园林绿化" in short[0]["raw_line"]


def test_sentinel_tax_not_blank(run):
    """TRAP-07 空值哨兵：空串表示无税号，哨兵另记标记，两者不能混为一谈。"""
    records = {row["supplier_id"]: row for row in store.list_records(run["run_id"])}
    blank = records["S-0042"]
    sentinel = records["S-0043"]
    unknown = records["S-0045"]
    assert blank["tax_id"] == ""
    assert blank["flags"] == "[]"
    assert sentinel["tax_id"] == ""
    assert "F_TAX_SENTINEL" in sentinel["flags"]
    assert "F_TAX_SENTINEL" in unknown["flags"]
    assert Decimal(unknown["ytd_amount"]) == Decimal("0.00")
    assert "F_AMOUNT_SENTINEL" in unknown["flags"]


def test_leading_zero_supplier_no_preserved(run):
    """TRAP-08 前导零：供应商编码按字符串保形，不经过 int()。"""
    records = {row["supplier_id"]: row for row in store.list_records(run["run_id"])}
    assert records["S-0001"]["supplier_no"] == "000101"
    assert records["S-0060"]["supplier_no"] == "000160"
    assert records["S-0007"]["supplier_no"].startswith("00")


def test_duplicate_id_conflict_kept(run):
    """TRAP-14 同号不同值：保留先到并记录被剔除来源，不静默覆盖。"""
    conflicts = store.list_conflicts(run["run_id"])
    assert len(conflicts) == EXP.CONFLICTS
    conflict = conflicts[0]
    assert conflict["supplier_id"] == "S-0060"
    assert conflict["field"] == "city"
    assert conflict["kept"] == "erp_d_amounts.csv:4"
    assert conflict["rejected"] == "erp_d_amounts.csv:5"
    record = store.get_record(run["run_id"], "S-0060")
    assert record["city"] == "大连"


def test_duplicate_identical_row_isolated(run):
    rows = store.list_quarantine(run["run_id"])
    duplicate = [row for row in rows if row["reason_code"] == "Q_DUP_ID_SAME"]
    assert len(duplicate) == 1
    assert duplicate[0]["line_no"] == 8
    assert "erp_d_amounts.csv:7" in duplicate[0]["detail"]


def test_empty_states_distinct(db):
    """TRAP-18 空态三态：仅表头、全空行、正常文件给出三种不同结论。"""
    header_only = service.ingest(files=["erp_e_header_only.csv"])
    blank_rows = service.ingest(files=["erp_f_blank_rows.csv"])
    normal = service.ingest(files=["erp_a_primary_utf8.csv"])

    header_file = header_only["files_json"]
    blank_file = blank_rows["files_json"]
    normal_file = normal["files_json"]
    assert '"empty_state": "header_only"' in header_file
    assert '"empty_state": "blank_rows"' in blank_file
    assert '"empty_state": ""' in normal_file
    assert store.count(header_only["run_id"], "records") == 0
    assert store.count(blank_rows["run_id"], "records") == 0
    assert store.count(normal["run_id"], "records") == 20


def test_missing_file_returns_404(client):
    response = client.post("/api/suppliers/import", json={"files": ["not_here.csv"]})
    assert response.status_code == 404
    assert response.json()["reason_code"] == "E_FILE_MISSING"


def test_full_sample_counts(run):
    counts = service.summary(run["run_id"])["counts"]
    assert counts["input_rows"] == EXP.INPUT_ROWS
    assert counts["accepted"] == EXP.ACCEPTED
    assert counts["quarantined"] == EXP.QUARANTINED
    assert counts["conflicts"] == EXP.CONFLICTS
    assert counts["input_rows"] == counts["accepted"] + counts["quarantined"] + counts["conflicts"]
    assert run["input_sha256"] == run["input_sha256"].lower()
    assert len(run["input_sha256"]) == 64


def test_import_from_directory(db, tmp_path):
    """跨批次演示需要从任意目录采信；目录只影响读取位置，批次号仍由文件摘要决定。"""
    import shutil

    from app.config import SAMPLE_DIR, SAMPLE_FILES

    target = tmp_path / "batch"
    target.mkdir()
    for name in SAMPLE_FILES:
        shutil.copy(SAMPLE_DIR / name, target / name)
    run = service.ingest(directory=str(target))
    assert run["run_id"] == EXP.RUN_ID
    assert run["counts_json"] == service.ingest()["counts_json"]


def test_legacy_database_gets_new_columns(tmp_path):
    """老库升级：已有表自动补 origin/carry 列，旧决定按人工处理。"""
    import sqlite3

    from app import store

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE decisions (run_id TEXT NOT NULL, pair_id TEXT NOT NULL, decision TEXT NOT NULL,
          expected_version INTEGER NOT NULL, version INTEGER NOT NULL, operator TEXT NOT NULL,
          decided_at TEXT NOT NULL, PRIMARY KEY (run_id, pair_id));
        INSERT INTO decisions VALUES ('R', 'P-1', 'keep', 0, 1, '王核', '2026-01-01T00:00:00Z');
        CREATE TABLE pair_scores (run_id TEXT NOT NULL, pair_id TEXT NOT NULL, left_id TEXT NOT NULL,
          right_id TEXT NOT NULL, dice TEXT NOT NULL, ratio TEXT NOT NULL, score TEXT NOT NULL,
          verdict TEXT NOT NULL, reason_code TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (run_id, pair_id));
        """
    )
    conn.commit()
    conn.close()

    store.init_db(path)
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(decisions)")}
    assert {"origin", "source_version", "carry_code"} <= columns
    origin = conn.execute("SELECT origin FROM decisions").fetchone()[0]
    assert origin == "manual"
    pair_columns = {row[1] for row in conn.execute("PRAGMA table_info(pair_scores)")}
    assert {"carry_status", "carry_code", "carry_detail"} <= pair_columns
    conn.close()

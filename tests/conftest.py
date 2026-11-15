"""测试基线：固定时区、每个测试独立 SQLite、样例批次夹具。"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["TZ"] = "UTC"
os.environ.setdefault("MERGE_DB", str(pathlib.Path(tempfile.mkdtemp()) / "session.db"))

import pytest  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MERGE_DB", str(tmp_path / "merge.db"))
    from app import store
    store.init_db()
    yield store


@pytest.fixture()
def run(db):
    from app.modules.m1_intake import service
    return service.ingest()


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def batch_factory(tmp_path):
    """把样例复制到临时目录并按需改写，供跨批次测试构造"下一月"名录。"""
    import shutil

    from app.config import SAMPLE_DIR, SAMPLE_FILES

    def build(name: str, mutations=None, extra_rows=None, stamp="2026-05") -> str:
        """stamp 改写末列日期，保证生成的是新批次而不是同一批重跑。"""
        target = tmp_path / name
        target.mkdir(exist_ok=True)
        for source in SAMPLE_FILES:
            shutil.copy(SAMPLE_DIR / source, target / source)
            if stamp:
                path = target / source
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                path.write_text(text.replace(",2026-03-", f",{stamp}-"),
                                encoding="utf-8")
        for filename, replacements in (mutations or {}).items():
            path = target / filename
            text = path.read_text(encoding="utf-8")
            for old, new in replacements:
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8")
        for filename, row in (extra_rows or {}).items():
            path = target / filename
            with path.open("a", encoding="utf-8") as handle:
                handle.write(row + "\n")
        return str(target)

    return build

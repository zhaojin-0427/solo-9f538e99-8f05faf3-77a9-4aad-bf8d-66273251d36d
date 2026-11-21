"""pytest 夹具：每个用例独立临时 SQLite 库，通过 TestClient 走完整 HTTP 链路。"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="vib-test-")
    db_path = os.path.join(tmp_dir, "test.db")
    monkeypatch.setenv("APP_DB_PATH", db_path)

    # 延迟导入，确保 app 读取到被 monkeypatch 的 APP_DB_PATH
    from app.main import app

    with TestClient(app) as c:
        yield c, db_path

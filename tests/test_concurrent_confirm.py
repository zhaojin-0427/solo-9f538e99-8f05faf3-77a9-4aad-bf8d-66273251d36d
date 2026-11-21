"""并发确认与交错提交测试。

覆盖需求：
- 两个窗口同时确认同一张预览：只有一笔成功拿到确认号，另一笔收到"已确认"及原号，
  原确认号始终可查，发号器只消耗一个序号；
- 影响面重算（只读阶段）期间，日分区 / 特征窗口 / 评分切片登记仍可提交；
- 只读阶段与写锁之间插入的相关登记（含新增命中窗口/切片）触发"预览过期"，
  无关登记不影响确认；
- 并发下保存失败不占号、不留单。
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import app.database as db_module
import app.services as services
from app.database import get_conn as real_get_conn
from tests.helpers import make_preview, put_partition, put_window, seed_base


class _BeginBarrierConn:
    """在 ``BEGIN IMMEDIATE``（拿写锁）处暂停的连接包装，用于制造并发竞态。"""

    def __init__(self, real, barrier):
        self._real = real
        self._barrier = barrier

    def __getattr__(self, name):
        return getattr(self._real, name)

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
            self._barrier.wait()
        return self._real.execute(sql, params)


class _FailingCommitConn:
    """提交即失败的连接：用于验证回滚后不占号、不留单。"""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        raise RuntimeError("disk full")


def _post_confirm(client, preview_id, results, idx):
    r = client.post(f"/api/previews/{preview_id}/confirm")
    results[idx] = (r.status_code, r.json())


def test_concurrent_confirm_same_preview_single_number(client):
    c, db_path = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    # 三个独立连接（模拟三个网页窗口），在 BEGIN IMMEDIATE 处对齐后同时抢写锁
    barrier = threading.Barrier(3)
    clients = []
    patch = pytest.MonkeyPatch()
    try:
        for _ in range(3):
            cc = TestClient(c.app)
            clients.append(cc)
        patch.setattr(
            db_module,
            "get_conn",
            lambda: _BeginBarrierConn(real_get_conn(), barrier),
        )

        results: list[tuple] = [None, None, None]  # type: ignore[list-item]
        threads = [
            threading.Thread(target=_post_confirm, args=(cc, preview_id, results, i))
            for i, cc in enumerate(clients)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        patch.undo()
        for cc in clients:
            cc.close()

    statuses = [r[0] for r in results]
    assert statuses.count(200) == 1, results
    assert statuses.count(409) == 2, results

    success = next(body for status, body in results if status == 200)
    confirmation_no = success["confirmation_no"]
    for status, body in results:
        if status == 409:
            # 后到者：提示已确认，并在消息与结构化 details 中给出原号
            assert body["error"]["details"]["reason"] == "already_confirmed"
            assert body["error"]["details"]["confirmation_no"] == confirmation_no
            assert confirmation_no in body["error"]["message"]

    # 每张预览最多一个号：库里只落一张确认单、只消耗一个序号
    conn = real_get_conn()
    try:
        row = conn.execute(
            "SELECT status, confirmation_no FROM preview WHERE id = ?",
            (preview_id,),
        ).fetchone()
        assert tuple(row) == ("confirmed", confirmation_no)
        assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 1
    finally:
        conn.close()

    # 先发（唯一）的号随后仍可查到原补算清单
    order = c.get(f"/api/orders/{confirmation_no}")
    assert order.status_code == 200
    assert order.json()["confirmation_no"] == confirmation_no
    assert len(order.json()["windows"]) == 6
    # 同一预览再次确认仍是"已确认 + 原号"
    again = c.post(f"/api/previews/{preview_id}/confirm")
    assert again.status_code == 409
    assert again.json()["error"]["details"]["confirmation_no"] == confirmation_no


def test_concurrent_confirm_different_previews_each_gets_number(client):
    c, _ = client
    seed_base(c)
    corrections = [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    id1, _ = make_preview(c, corrections)
    id2, _ = make_preview(c, corrections)

    barrier = threading.Barrier(2)
    cc1, cc2 = TestClient(c.app), TestClient(c.app)
    patch = pytest.MonkeyPatch()
    results: list[tuple] = [None, None]  # type: ignore[list-item]
    try:
        patch.setattr(
            db_module,
            "get_conn",
            lambda: _BeginBarrierConn(real_get_conn(), barrier),
        )
        threads = [
            threading.Thread(target=_post_confirm, args=(cc1, id1, results, 0)),
            threading.Thread(target=_post_confirm, args=(cc2, id2, results, 1)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        patch.undo()
        cc1.close()
        cc2.close()

    assert [r[0] for r in results] == [200, 200], results
    numbers = {r[1]["confirmation_no"] for r in results}
    assert len(numbers) == 2
    conn = real_get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 2
    finally:
        conn.close()


def test_registrations_commit_during_readonly_recompute_phase(client, monkeypatch):
    c, db_path = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    entered = threading.Event()
    release = threading.Event()
    real_compute = services.compute_impact

    def slow_compute(conn, corrections):
        entered.set()
        assert release.wait(timeout=10)
        return real_compute(conn, corrections)

    monkeypatch.setattr(services, "compute_impact", slow_compute)
    quiet = TestClient(c.app, raise_server_exceptions=False)
    confirm_out: dict = {}

    def worker():
        r = quiet.post(f"/api/previews/{preview_id}/confirm")
        confirm_out["status"] = r.status_code
        confirm_out["body"] = r.json()

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert entered.wait(timeout=10)  # 确认请求正处在只读影响面重算中

        # 重算期间：日分区、特征窗口、评分切片登记都必须照常提交（写不被读阻塞）
        r1 = c.post(
            "/api/partitions",
            json={"turbine_id": "WT-01", "utc_date": "2026-11-16", "revision": 1},
        )
        assert r1.status_code == 201
        r2 = c.post(
            "/api/windows",
            json={
                "turbine_id": "WT-01",
                "feature_name": "vib_rms",
                "end_date": "2026-11-20",
            },
        )
        assert r2.status_code == 201
        new_window = r2.json()
        r3 = c.post(
            "/api/slices",
            json={
                "turbine_id": "WT-01",
                "slice_name": "other_alert",
                "window_refs": [
                    {
                        "turbine_id": new_window["turbine_id"],
                        "feature_name": new_window["feature_name"],
                        "end_date": new_window["end_date"],
                        "version": new_window["version"],
                    }
                ],
            },
        )
        assert r3.status_code == 201
    finally:
        release.set()
        t.join(timeout=30)
        quiet.close()

    # 上述登记均不覆盖更正日 11-13，属无关改动：确认仍成功
    assert confirm_out["status"] == 200, confirm_out
    assert confirm_out["body"]["confirmation_no"]


def test_related_registration_between_phases_marks_preview_stale(client):
    c, db_path = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    # 在写锁前一刻插入一笔相关登记：命中窗口出现新版本 -> 锁内水位变化触发重算 -> 过期
    def begin_with_related_change(real):
        class _Conn:
            def __init__(self):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    put_window(c, "WT-01", "vib_rms", "2026-11-13")  # version 2
                return self._real.execute(sql, params)

        return _Conn()

    # 独立补丁上下文：undo 时不影响夹具设置的 APP_DB_PATH
    patch = pytest.MonkeyPatch()
    patch.setattr(
        db_module, "get_conn", lambda: begin_with_related_change(real_get_conn())
    )
    quiet = TestClient(c.app, raise_server_exceptions=False)
    r = quiet.post(f"/api/previews/{preview_id}/confirm")
    quiet.close()
    assert r.status_code == 409
    err = r.json()["error"]
    assert "预览已过期" in err["message"]
    assert err["details"]["reason"] == "preview_stale"
    assert "window_ids" in err["details"]["diff"]

    # 被拒不占号；重新生成预览后可正常确认
    conn = real_get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM preview WHERE id = ?", (preview_id,)
        ).fetchone()[0] == "open"
    finally:
        conn.close()
    patch.undo()
    new_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    ok = c.post(f"/api/previews/{new_id}/confirm")
    assert ok.status_code == 200 and ok.json()["confirmation_no"]


def test_newly_hitting_partition_between_phases_marks_stale(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    # 锁前插入源分区新修订：指纹中三日源修订 id 集合变化（新增命中的源修订）
    def begin_with_partition_change(real):
        class _Conn:
            def __init__(self):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    put_partition(c, "WT-01", "2026-11-13", 2)
                return self._real.execute(sql, params)

        return _Conn()

    patch = pytest.MonkeyPatch()
    patch.setattr(
        db_module, "get_conn", lambda: begin_with_partition_change(real_get_conn())
    )
    quiet = TestClient(c.app, raise_server_exceptions=False)
    r = quiet.post(f"/api/previews/{preview_id}/confirm")
    quiet.close()
    patch.undo()
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["details"]["reason"] == "preview_stale"
    assert "partition_ids" in err["details"]["diff"]


def test_unrelated_registration_between_phases_still_confirms(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    # 锁前插入无关风机登记：水位变化触发锁内重算，但指纹一致 -> 照常确认
    def begin_with_unrelated_change(real):
        class _Conn:
            def __init__(self):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def execute(self, sql, params=()):
                if sql.strip().upper().startswith("BEGIN IMMEDIATE"):
                    put_partition(c, "WT-99", "2026-11-13", 1)
                return self._real.execute(sql, params)

        return _Conn()

    patch = pytest.MonkeyPatch()
    patch.setattr(
        db_module, "get_conn", lambda: begin_with_unrelated_change(real_get_conn())
    )
    quiet = TestClient(c.app, raise_server_exceptions=False)
    r = quiet.post(f"/api/previews/{preview_id}/confirm")
    quiet.close()
    patch.undo()
    assert r.status_code == 200
    assert r.json()["confirmation_no"]
    conn = real_get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 1
    finally:
        conn.close()


def test_concurrent_save_failure_consumes_no_number(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    # 一个窗口提交即失败，另一个窗口正常确认：失败方回滚不占号，成功方只占一个号
    call_count = {"n": 0}
    lock = threading.Lock()

    def flaky_get_conn():
        with lock:
            call_count["n"] += 1
            idx = call_count["n"]
        real = real_get_conn()
        return _FailingCommitConn(real) if idx == 1 else real

    patch = pytest.MonkeyPatch()
    patch.setattr(db_module, "get_conn", flaky_get_conn)
    quiet = TestClient(c.app, raise_server_exceptions=False)
    failure: dict = {}
    t = threading.Thread(
        target=lambda: failure.update(
            {"r": quiet.post(f"/api/previews/{preview_id}/confirm")}
        )
    )
    t.start()
    t.join(timeout=30)
    quiet.close()
    patch.undo()
    assert failure["r"].status_code == 500

    ok = c.post(f"/api/previews/{preview_id}/confirm")
    assert ok.status_code == 200
    confirmation_no = ok.json()["confirmation_no"]

    conn = real_get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 1
        row = conn.execute(
            "SELECT status, confirmation_no FROM preview WHERE id = ?",
            (preview_id,),
        ).fetchone()
        assert tuple(row) == ("confirmed", confirmation_no)
    finally:
        conn.close()
    assert c.get(f"/api/orders/{confirmation_no}").status_code == 200

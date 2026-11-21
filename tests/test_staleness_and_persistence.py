"""预览失效、重启复算、引用共用与事务原子性测试。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_preview, put_partition, put_window, seed_base


def _re_correct_partition(c):
    return put_partition(c, "WT-01", "2026-11-13", 2)


def test_source_partition_re_correction_rejects_old_preview(client):
    c, db_path = client
    seed_base(c)
    preview_id, impact = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    old_partition_ids = {
        p["partition_id"]
        for w in impact["windows"]
        for p in w["source_partitions"]
        if p["utc_date"] == "2026-11-13"
    }
    _re_correct_partition(c)
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 409
    assert "预览已过期" in r.json()["error"]["message"]
    # 差异中包含分区集合变化（新命中的源修订）
    assert "partition_ids" in r.json()["error"]["message"]
    assert old_partition_ids  # 旧引用仍在库中不可变

    # 重新生成预览即可确认
    new_id, new_impact = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    assert c.post(f"/api/previews/{new_id}/confirm").status_code == 200
    # 新预览已看到 rev2
    for window in new_impact["windows"]:
        src = next(p for p in window["source_partitions"] if p["utc_date"] == "2026-11-13")
        assert src["revision"] == 2


def test_new_window_version_hit_rejects_old_preview(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    # 依赖换版：命中窗口身份出现新版本
    put_window(c, "WT-01", "vib_rms", "2026-11-13")  # version 2
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 409
    assert "window_ids" in r.json()["error"]["message"]


def test_new_slice_version_rejects_old_preview(client):
    c, _ = client
    base = seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    # 切片换版：新切片引用同一批窗口（旧窗口仍命中，但切片身份变了）
    refs = [
        {
            "turbine_id": "WT-01",
            "feature_name": w["feature_name"],
            "end_date": w["end_date"],
            "version": w["version"],
        }
        for w in base["rms"] + base["crest"]
    ]
    r = c.post(
        "/api/slices",
        json={"turbine_id": "WT-01", "slice_name": "daily_alert", "window_refs": refs},
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 409
    assert "slice_ids" in r.json()["error"]["message"]


def test_same_turbine_unrelated_window_still_confirmable(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    # 同风机但窗口不覆盖任何更正日：属于无关改动
    put_window(c, "WT-01", "vib_rms", "2026-11-20")
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 200
    assert r.json()["confirmation_no"]


def test_restart_then_rebuild_original_order_by_confirmation_no(client):
    c, db_path = client
    base = seed_base(c)
    preview_id, impact = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    confirmed = c.post(f"/api/previews/{preview_id}/confirm").json()
    confirmation_no = confirmed["confirmation_no"]
    original_window_ids = sorted(w["id"] for w in impact["windows"])
    original_slice_id = base["slice"]["id"]
    original_partition_rev = {
        p["partition_id"]: p["revision"]
        for w in impact["windows"]
        for p in w["source_partitions"]
    }

    # 依赖换版 + 源分区再次更正
    put_partition(c, "WT-01", "2026-11-13", 2)
    for w in base["rms"] + base["crest"]:
        put_window(c, "WT-01", w["feature_name"], w["end_date"])
    v2_windows = c.get("/api/windows").json()
    refs_v2 = [
        {
            "turbine_id": w["turbine_id"],
            "feature_name": w["feature_name"],
            "end_date": w["end_date"],
            "version": w["version"],
        }
        for w in v2_windows
    ]
    c.post(
        "/api/slices",
        json={"turbine_id": "WT-01", "slice_name": "daily_alert", "window_refs": refs_v2},
    )

    # 模拟服务重启：全新 TestClient / 全新连接，仅靠磁盘上的 SQLite 文件
    from app.main import app

    restarted = TestClient(app)
    r = restarted.get(f"/api/orders/{confirmation_no}")
    assert r.status_code == 200
    order = r.json()
    assert order["rebuilt_from_retained_refs"] is True
    assert order["confirmation_no"] == confirmation_no

    # 留存的仍是旧窗口、旧切片、旧源修订
    assert order["retained_refs"]["window_ids"] == original_window_ids
    assert order["retained_refs"]["slice_ids"] == [original_slice_id]
    assert all(w["version"] == 1 for w in order["windows"])
    assert all(s["version"] == 1 for s in order["slices"])
    for window in order["windows"]:
        for src in window["source_partitions"]:
            assert src["revision"] == original_partition_rev[src["partition_id"]] == 1

    # 每个切片任务列出命中窗口及源修订号
    slice_task = order["slice_tasks"][0]
    assert sorted(hw["window_id"] for hw in slice_task["hit_windows"]) == original_window_ids
    for hw in slice_task["hit_windows"]:
        assert {p["revision"] for p in hw["source_partitions"]} == {1}
    # 任务顺序仍是先特征后切片
    assert [t["kind"] for t in order["tasks"]] == (
        ["feature"] * len(order["feature_tasks"]) + ["slice"] * len(order["slice_tasks"])
    )

    # 同一确认号再查、按预览 id 查，结果一致
    again = restarted.get(f"/api/orders/{confirmation_no}").json()
    by_id = restarted.get(f"/api/previews/{preview_id}").json()
    assert again["retained_refs"] == order["retained_refs"]
    assert by_id["confirmation_no"] == confirmation_no

    # 重启后新预览看到的是新版本，证明当前状态已推进而旧单不受影响
    fresh = restarted.post(
        "/api/previews",
        json={"corrections": [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]},
    ).json()
    assert all(w["version"] == 2 for w in fresh["impact"]["windows"])
    assert fresh["impact"]["slices"][0]["version"] == 2
    assert any(
        p["revision"] == 2
        for w in fresh["impact"]["windows"]
        for p in w["source_partitions"]
        if p["utc_date"] == "2026-11-13"
    )


def test_same_version_records_shared_between_orders(client):
    c, _ = client
    seed_base(c)
    corrections = [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    id1, impact1 = make_preview(c, corrections)
    c.post(f"/api/previews/{id1}/confirm")
    id2, impact2 = make_preview(c, corrections)
    c.post(f"/api/previews/{id2}/confirm")

    # 无版本变化，两张单共用同一批同版记录
    assert [w["id"] for w in impact1["windows"]] == [w["id"] for w in impact2["windows"]]
    o1 = c.get(f"/api/previews/{id1}").json()
    o2 = c.get(f"/api/previews/{id2}").json()
    assert o1["retained_refs"] == o2["retained_refs"]
    assert o1["confirmation_no"] != o2["confirmation_no"]


def test_failed_save_leaves_no_order_and_no_confirmation_number(client, monkeypatch):
    import app.database as db_module
    from app.database import get_conn as real_get_conn

    c, db_path = client
    seed_base(c)

    class FailingConn:
        """提交即失败的连接包装：写入在事务内可见，commit 失败后全部回滚。"""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            raise RuntimeError("disk full")

    def failing_get_conn():
        return FailingConn(real_get_conn())

    quiet = TestClient(c.app, raise_server_exceptions=False)
    # 独立补丁上下文：undo 时不影响夹具设置的 APP_DB_PATH
    patch = pytest.MonkeyPatch()

    # 场景一：预览保存失败 -> 连预览行都不留
    patch.setattr(db_module, "get_conn", failing_get_conn)
    r = quiet.post(
        "/api/previews",
        json={"corrections": [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]},
    )
    assert r.status_code == 500
    patch.undo()

    conn = real_get_conn()
    assert conn.execute("SELECT COUNT(*) FROM preview").fetchone()[0] == 0
    conn.close()

    # 场景二：确认保存失败 -> 不产生确认单状态、不消耗确认号
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    patch.setattr(db_module, "get_conn", failing_get_conn)
    r = quiet.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 500
    patch.undo()

    conn = real_get_conn()
    row = conn.execute("SELECT status, confirmation_no FROM preview WHERE id = ?",
                       (preview_id,)).fetchone()
    assert tuple(row) == ("open", None)
    assert conn.execute("SELECT COUNT(*) FROM order_seq").fetchone()[0] == 0
    conn.close()

    # 故障恢复后同一预览仍可正常确认发号
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 200
    assert r.json()["confirmation_no"]


def test_empty_impact_preview_is_confirmable(client):
    c, _ = client
    seed_base(c)
    preview_id, impact = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-30"}]  # 无窗口覆盖
    )
    assert impact["windows"] == [] and impact["slices"] == []
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 200
    assert r.json()["confirmation_no"]


def test_unknown_ids_return_404(client):
    c, _ = client
    assert c.get("/api/previews/9999").status_code == 404
    assert c.get("/api/orders/CF-nope").status_code == 404
    assert c.post("/api/previews/9999/confirm").status_code == 404

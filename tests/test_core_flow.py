"""核心规则：三日覆盖命中、连续日期合并、先特征后切片、按所见版本确认发号。"""

from __future__ import annotations

from tests.helpers import make_preview, seed_base


def test_partition_revision_must_increase(client):
    c, _ = client
    r = c.post(
        "/api/partitions",
        json={"turbine_id": "WT-01", "utc_date": "2026-11-13", "revision": 1},
    )
    assert r.status_code == 201
    r = c.post(
        "/api/partitions",
        json={"turbine_id": "WT-01", "utc_date": "2026-11-13", "revision": 1},
    )
    assert r.status_code == 409
    r = c.post(
        "/api/partitions",
        json={"turbine_id": "WT-01", "utc_date": "2026-11-13", "revision": 2},
    )
    assert r.status_code == 201
    assert r.json()["revision"] == 2


def test_window_and_slice_version_increase(client):
    c, _ = client
    r = c.post(
        "/api/windows",
        json={
            "turbine_id": "WT-01",
            "feature_name": "f1",
            "end_date": "2026-11-13",
        },
    )
    assert r.status_code == 201 and r.json()["version"] == 1
    r = c.post(
        "/api/windows",
        json={
            "turbine_id": "WT-01",
            "feature_name": "f1",
            "end_date": "2026-11-13",
        },
    )
    assert r.status_code == 201 and r.json()["version"] == 2
    r = c.post(
        "/api/windows",
        json={
            "turbine_id": "WT-01",
            "feature_name": "f1",
            "end_date": "2026-11-13",
            "version": 2,
        },
    )
    assert r.status_code == 409


def test_slice_rejects_missing_or_foreign_window(client):
    c, _ = client
    r = c.post(
        "/api/slices",
        json={
            "turbine_id": "WT-01",
            "slice_name": "s1",
            "window_refs": [
                {
                    "turbine_id": "WT-01",
                    "feature_name": "ghost",
                    "end_date": "2026-11-13",
                    "version": 1,
                }
            ],
        },
    )
    assert r.status_code == 400

    # 别的风机的窗口不能挂到本风机切片上
    c.post(
        "/api/partitions",
        json={"turbine_id": "WT-02", "utc_date": "2026-11-13", "revision": 1},
    )
    foreign = c.post(
        "/api/windows",
        json={
            "turbine_id": "WT-02",
            "feature_name": "f1",
            "end_date": "2026-11-13",
        },
    ).json()
    r = c.post(
        "/api/slices",
        json={
            "turbine_id": "WT-01",
            "slice_name": "s1",
            "window_refs": [
                {
                    "turbine_id": foreign["turbine_id"],
                    "feature_name": foreign["feature_name"],
                    "end_date": foreign["end_date"],
                    "version": foreign["version"],
                }
            ],
        },
    )
    assert r.status_code == 400


def test_three_day_window_hit_and_merged_tasks(client):
    c, _ = client
    base = seed_base(c)

    # 更正 11-13：覆盖它的窗口结束日为 13/14/15（三日窗：[11,12,13]/[12,13,14]/[13,14,15]）
    preview_id, impact = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )

    assert {w["end_date"] for w in impact["windows"]} == {
        "2026-11-13",
        "2026-11-14",
        "2026-11-15",
    }
    assert {w["feature_name"] for w in impact["windows"]} == {
        "vib_rms",
        "vib_crest",
    }
    # 每个窗口留存三日源分区修订
    for window in impact["windows"]:
        assert [p["utc_date"] for p in window["source_partitions"]] == [
            *window["covered_dates"]
        ]
        assert all(p["revision"] == 1 for p in window["source_partitions"])

    # 两个特征各自的三个连续结束日合并为一个特征任务
    assert len(impact["feature_tasks"]) == 2
    for task in impact["feature_tasks"]:
        assert task["kind"] == "feature"
        assert task["end_date_from"] == "2026-11-13"
        assert task["end_date_to"] == "2026-11-15"
        assert len(task["window_ids"]) == 3
    assert [t["feature_name"] for t in impact["feature_tasks"]] == [
        "vib_crest",
        "vib_rms",
    ]

    # 切片任务在特征任务之后，且记录命中窗口
    assert len(impact["slice_tasks"]) == 1
    slice_task = impact["slice_tasks"][0]
    assert slice_task["slice_id"] == base["slice"]["id"]
    assert len(slice_task["hit_windows"]) == 6
    assert impact["tasks"] == impact["feature_tasks"] + impact["slice_tasks"]

    # 确认成功，发确认号
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 200, r.text
    confirmation_no = r.json()["confirmation_no"]
    assert confirmation_no.startswith("CF-")
    # 重复确认被拒
    assert c.post(f"/api/previews/{preview_id}/confirm").status_code == 409

    # 列表中状态更新
    listed = c.get("/api/previews").json()
    row = next(x for x in listed if x["id"] == preview_id)
    assert row["status"] == "confirmed"
    assert row["confirmation_no"] == confirmation_no


def test_non_consecutive_dates_split_into_two_feature_tasks(client):
    c, _ = client
    base = seed_base(c)
    # 更正 11-11 与 11-15：rms 命中窗口为结束日 13（覆盖11）与 15（覆盖15），14 不命中
    _, impact = make_preview(
        c,
        [
            {"turbine_id": "WT-01", "utc_date": "2026-11-11"},
            {"turbine_id": "WT-01", "utc_date": "2026-11-15"},
        ],
    )
    rms_tasks = [t for t in impact["feature_tasks"] if t["feature_name"] == "vib_rms"]
    assert len(rms_tasks) == 2
    assert [t["end_date_from"] for t in rms_tasks] == ["2026-11-13", "2026-11-15"]
    # 11-15 只被结束日 15 的窗口覆盖（14 号窗覆盖 12/13/14）
    hit_ends = {w["end_date"] for w in impact["windows"] if w["feature_name"] == "vib_rms"}
    assert hit_ends == {"2026-11-13", "2026-11-15"}


def test_unrelated_turbine_change_does_not_invalidate_preview(client):
    c, _ = client
    seed_base(c)
    preview_id, _ = make_preview(
        c, [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]
    )
    # 无关风机的任何改动
    c.post(
        "/api/partitions",
        json={"turbine_id": "WT-99", "utc_date": "2026-11-13", "revision": 7},
    )
    c.post(
        "/api/windows",
        json={
            "turbine_id": "WT-99",
            "feature_name": "other",
            "end_date": "2026-11-13",
        },
    )
    r = c.post(f"/api/previews/{preview_id}/confirm")
    assert r.status_code == 200
    assert r.json()["confirmation_no"]

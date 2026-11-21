"""测试辅助：快速构造基础数据集。"""

from __future__ import annotations


def put_partition(client, turbine: str, day: str, revision: int) -> dict:
    r = client.post(
        "/api/partitions",
        json={"turbine_id": turbine, "utc_date": day, "revision": revision},
    )
    assert r.status_code == 201, r.text
    return r.json()


def put_window(
    client, turbine: str, feature: str, end: str, version: int | None = None
) -> dict:
    body = {
        "turbine_id": turbine,
        "feature_name": feature,
        "end_date": end,
    }
    if version is not None:
        body["version"] = version
    r = client.post("/api/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def put_slice(
    client,
    turbine: str,
    name: str,
    refs: list[dict],
    version: int | None = None,
) -> dict:
    body = {"turbine_id": turbine, "slice_name": name, "window_refs": refs}
    if version is not None:
        body["version"] = version
    r = client.post("/api/slices", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_preview(client, corrections: list[dict]) -> tuple[int, dict]:
    r = client.post("/api/previews", json={"corrections": corrections})
    assert r.status_code == 201, r.text
    data = r.json()
    return data["preview_id"], data["impact"]


def seed_base(client) -> dict:
    """构造 5 天分区、两个特征各 3 个连续结束日窗口、一个引用全部窗口的切片。"""
    days = [f"2026-11-{d:02d}" for d in range(11, 16)]
    for day in days:
        put_partition(client, "WT-01", day, 1)

    ends = ["2026-11-13", "2026-11-14", "2026-11-15"]
    rms_windows = [put_window(client, "WT-01", "vib_rms", e) for e in ends]
    crest_windows = [put_window(client, "WT-01", "vib_crest", e) for e in ends]

    refs = [
        {
            "turbine_id": "WT-01",
            "feature_name": w["feature_name"],
            "end_date": w["end_date"],
            "version": w["version"],
        }
        for w in rms_windows + crest_windows
    ]
    slice_row = put_slice(client, "WT-01", "daily_alert", refs)
    return {
        "days": days,
        "rms": rms_windows,
        "crest": crest_windows,
        "slice": slice_row,
    }

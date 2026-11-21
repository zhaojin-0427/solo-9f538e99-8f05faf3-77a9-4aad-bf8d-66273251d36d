#!/usr/bin/env python3
"""端到端冒烟脚本：用标准库 urllib 走完整业务剧情。

剧情：
1. 上传三日分区修订、登记窗口与切片；
2. 更正 11-13 -> 预览 -> 确认拿确认号；
3. 源分区再次更正 -> 旧预览被拒、无关改动可确认；
4. 换版并"重启"（新建连接视角）-> 按确认号复算原清单，核对留存旧版本。

用法: python scripts/smoke.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{(' -> ' + detail) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def main(base: str) -> None:
    # 1. 分区修订
    for day in ("2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"):
        status, _ = call("POST", f"{base}/api/partitions",
                         {"turbine_id": "WT-01", "utc_date": day, "revision": 1})
        check(f"上传分区 {day} rev1", status == 201)
    status, body = call("POST", f"{base}/api/partitions",
                        {"turbine_id": "WT-01", "utc_date": "2026-11-13", "revision": 1})
    check("重复修订号被拒(409)", status == 409, body.get("error", {}).get("code", ""))

    # 2. 窗口（两个特征 × 三个连续结束日）
    windows = []
    for feature in ("vib_rms", "vib_crest"):
        for end in ("2026-11-13", "2026-11-14", "2026-11-15"):
            status, body = call("POST", f"{base}/api/windows", {
                "turbine_id": "WT-01", "feature_name": feature, "end_date": end})
            check(f"登记窗口 {feature} {end}", status == 201)
            windows.append(body)

    # 3. 切片引用全部窗口
    refs = [{"turbine_id": w["turbine_id"], "feature_name": w["feature_name"],
             "end_date": w["end_date"], "version": w["version"]} for w in windows]
    status, slice_v1 = call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "daily_alert", "window_refs": refs})
    check("登记切片 v1", status == 201 and slice_v1["version"] == 1)

    # 4. 预览 + 确认
    status, preview = call("POST", f"{base}/api/previews", {
        "corrections": [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]})
    check("生成预览", status == 201)
    impact = preview["impact"]
    check("命中 6 个窗口", len(impact["windows"]) == 6)
    check("合并为 2 个连续特征任务", len(impact["feature_tasks"]) == 2)
    check("先特征后切片",
          [t["kind"] for t in impact["tasks"]] == ["feature", "feature", "slice"])
    preview_id = preview["preview_id"]

    # 无关改动：别的风机的数据不影响确认
    call("POST", f"{base}/api/partitions",
         {"turbine_id": "WT-99", "utc_date": "2026-11-13", "revision": 1})
    status, confirmed = call("POST", f"{base}/api/previews/{preview_id}/confirm")
    check("无关改动后确认成功", status == 200, confirmed.get("confirmation_no", ""))
    confirmation_no = confirmed["confirmation_no"]

    # 5. 新预览后，源分区再次更正 -> 旧预览拒绝
    status, preview2 = call("POST", f"{base}/api/previews", {
        "corrections": [{"turbine_id": "WT-01", "utc_date": "2026-11-13"}]})
    pid2 = preview2["preview_id"]
    call("POST", f"{base}/api/partitions",
         {"turbine_id": "WT-01", "utc_date": "2026-11-13", "revision": 2})
    status, body = call("POST", f"{base}/api/previews/{pid2}/confirm")
    check("源分区再更正后旧预览被拒(409)", status == 409)

    # 6. 全部换版后按确认号复算旧单，内容仍是旧版本
    call("POST", f"{base}/api/partitions",
         {"turbine_id": "WT-01", "utc_date": "2026-11-14", "revision": 2})
    for feature in ("vib_rms", "vib_crest"):
        for end in ("2026-11-13", "2026-11-14", "2026-11-15"):
            call("POST", f"{base}/api/windows",
                 {"turbine_id": "WT-01", "feature_name": feature, "end_date": end})
    status, all_windows = call("GET", f"{base}/api/windows")
    v2_refs = [{"turbine_id": w["turbine_id"], "feature_name": w["feature_name"],
                "end_date": w["end_date"], "version": w["version"]}
               for w in all_windows]
    call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "daily_alert", "window_refs": v2_refs})

    status, order = call("GET", f"{base}/api/orders/{confirmation_no}")
    check("按确认号打开旧单", status == 200 and order["confirmation_no"] == confirmation_no)
    check("旧单留存窗口均为 v1", all(w["version"] == 1 for w in order["windows"]))
    check("旧单留存切片为 v1", all(s["version"] == 1 for s in order["slices"]))
    check("旧单源修订号均为 rev1",
          all(p["revision"] == 1 for w in order["windows"] for p in w["source_partitions"]))
    check("逐切片列出命中窗口与源修订",
          len(order["slice_tasks"][0]["hit_windows"]) == 6
          and all(hw["source_partitions"] for hw in order["slice_tasks"][0]["hit_windows"]))
    check("标记为留存引用复算", order["rebuilt_from_retained_refs"] is True)

    print(f"\n冒烟通过，确认号: {confirmation_no}")


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    main(base_url.rstrip("/"))

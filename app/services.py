"""核心领域逻辑与事务服务。

关键规则：
1. 窗口取结束日及前两日；某风机某日的分区被更正，所有覆盖该日的同风机最新版窗口命中。
2. 命中窗口被任一最新版评分切片引用，则该切片命中。
3. 同一特征连续结束日的窗口合并为一个特征任务；任务顺序为先特征、后切片，去重。
4. 预览时固化"所见版本指纹"（窗口 id、切片 id、三日源分区修订 id、切片-窗口引用对），
   确认时按当前已提交状态重算指纹，任一集合变化即拒绝旧预览；无关改动不影响确认。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .errors import BadRequestError, ConflictError, NotFoundError
from .schemas import CorrectionIn, PartitionIn, SliceIn, WindowIn


# --------------------------------------------------------------------------- #
# 日期工具
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:  # pragma: no cover - schema 已做格式校验
        raise BadRequestError(f"非法 UTC 日期: {value}") from exc


def three_days(end_date: str) -> list[str]:
    """窗口覆盖结束日及前两日，按日期升序返回。"""
    d = parse_date(end_date)
    return [(d - timedelta(days=i)).isoformat() for i in (2, 1, 0)]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def group_consecutive(dates: list[str]) -> list[tuple[str, str]]:
    """把升序日期序列切分为连续日区间，返回 [(区间起, 区间止), ...]。"""
    if not dates:
        return []
    ordered = sorted(dates)
    groups: list[list[str]] = [[ordered[0]]]
    prev = parse_date(ordered[0])
    for value in ordered[1:]:
        cur = parse_date(value)
        if (cur - prev).days == 1:
            groups[-1].append(value)
        else:
            groups.append([value])
        prev = cur
    return [(g[0], g[-1]) for g in groups]


# --------------------------------------------------------------------------- #
# 影响面计算（纯读，基于当前已提交状态）
# --------------------------------------------------------------------------- #
def _latest_partition_map(
    conn: sqlite3.Connection, turbine_id: str, days: list[str]
) -> dict[str, sqlite3.Row]:
    """取某风机若干日期上各自最新的分区修订。"""
    if not days:
        return {}
    placeholders = ",".join("?" for _ in days)
    rows = conn.execute(
        f"""
        SELECT pr.*
        FROM partition_rev pr
        JOIN (
            SELECT utc_date, MAX(revision) AS max_rev
            FROM partition_rev
            WHERE turbine_id = ? AND utc_date IN ({placeholders})
            GROUP BY utc_date
        ) m ON m.utc_date = pr.utc_date AND m.max_rev = pr.revision
        WHERE pr.turbine_id = ?
        """,
        (turbine_id, *days, turbine_id),
    ).fetchall()
    return {r["utc_date"]: r for r in rows}


def _latest_windows(conn: sqlite3.Connection) -> dict[tuple, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM feature_window").fetchall()
    latest: dict[tuple, sqlite3.Row] = {}
    for row in rows:
        key = (row["turbine_id"], row["feature_name"], row["end_date"])
        if key not in latest or row["version"] > latest[key]["version"]:
            latest[key] = row
    return latest


def _latest_slices(conn: sqlite3.Connection) -> dict[tuple, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM scoring_slice").fetchall()
    latest: dict[tuple, sqlite3.Row] = {}
    for row in rows:
        key = (row["turbine_id"], row["slice_name"])
        if key not in latest or row["version"] > latest[key]["version"]:
            latest[key] = row
    return latest


def compute_impact(
    conn: sqlite3.Connection, corrections: list[CorrectionIn]
) -> dict:
    """按当前已提交状态计算更正影响面与补算任务清单。"""
    # 归一化更正点（去重）
    corr_dates: dict[str, set[str]] = defaultdict(set)
    for item in corrections:
        parse_date(item.utc_date)  # 校验
        corr_dates[item.turbine_id].add(item.utc_date)

    # ---- 命中窗口：最新版窗口的三日覆盖与更正日相交 ----
    hit_windows: list[dict] = []
    window_sources: dict[int, list[dict]] = {}
    for window in _latest_windows(conn).values():
        if window["turbine_id"] not in corr_dates:
            continue
        covered = three_days(window["end_date"])
        if not any(d in corr_dates[window["turbine_id"]] for d in covered):
            continue
        source_rows = _latest_partition_map(conn, window["turbine_id"], covered)
        sources = [
            {
                "partition_id": source_rows[d]["id"],
                "utc_date": d,
                "revision": source_rows[d]["revision"],
            }
            for d in covered
            if d in source_rows
        ]
        record = dict(window)
        record["covered_dates"] = covered
        record["source_partitions"] = sources
        hit_windows.append(record)
        window_sources[window["id"]] = sources

    hit_windows.sort(
        key=lambda w: (w["turbine_id"], w["feature_name"], w["end_date"])
    )
    hit_window_ids = {w["id"] for w in hit_windows}

    # ---- 命中切片：最新版切片引用了任一命中窗口 ----
    hit_slices: list[dict] = []
    for slice_row in _latest_slices(conn).values():
        ref_rows = conn.execute(
            "SELECT window_id FROM slice_window_ref WHERE slice_id = ?",
            (slice_row["id"],),
        ).fetchall()
        ref_ids = [r["window_id"] for r in ref_rows]
        matched = sorted(wid for wid in ref_ids if wid in hit_window_ids)
        if not matched:
            continue
        record = dict(slice_row)
        record["hit_window_ids"] = matched
        hit_slices.append(record)
    hit_slices.sort(key=lambda s: (s["turbine_id"], s["slice_name"]))

    feature_tasks, slice_tasks = build_tasks(
        hit_windows, hit_slices, window_sources
    )
    return {
        "corrections": sorted(
            (
                {"turbine_id": t, "utc_date": d}
                for t, dates in corr_dates.items()
                for d in dates
            ),
            key=lambda item: (item["turbine_id"], item["utc_date"]),
        ),
        "windows": hit_windows,
        "slices": hit_slices,
        "feature_tasks": feature_tasks,
        "slice_tasks": slice_tasks,
        "tasks": feature_tasks + slice_tasks,
    }


def _window_brief(window: dict, sources: list[dict]) -> dict:
    return {
        "window_id": window["id"],
        "feature_name": window["feature_name"],
        "end_date": window["end_date"],
        "version": window["version"],
        "source_partitions": sources,
    }


def build_tasks(
    hit_windows: list[dict],
    hit_slices: list[dict],
    window_sources: dict[int, list[dict]],
) -> tuple[list[dict], list[dict]]:
    """生成去重、排序后的特征任务与切片任务。"""
    # 特征任务：按 (风机, 特征) 分组，再按连续结束日切段
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for window in hit_windows:
        grouped[(window["turbine_id"], window["feature_name"])].append(window)

    feature_tasks: list[dict] = []
    for (turbine_id, feature_name), windows in sorted(grouped.items()):
        by_date = {w["end_date"]: w for w in windows}
        for start, end in group_consecutive(list(by_date.keys())):
            in_range = [
                w
                for w in windows
                if start <= w["end_date"] <= end
            ]
            partition_index: dict[int, dict] = {}
            for w in in_range:
                for src in window_sources.get(w["id"], []):
                    partition_index[src["partition_id"]] = src
            feature_tasks.append(
                {
                    "kind": "feature",
                    "turbine_id": turbine_id,
                    "feature_name": feature_name,
                    "end_date_from": start,
                    "end_date_to": end,
                    "window_ids": sorted(w["id"] for w in in_range),
                    "window_versions": sorted({w["version"] for w in in_range}),
                    "source_partitions": sorted(
                        partition_index.values(), key=lambda x: x["utc_date"]
                    ),
                }
            )

    # 切片任务：每个命中切片一个任务，附命中窗口与对应源修订号
    slice_tasks: list[dict] = []
    for slice_row in hit_slices:
        window_index = {w["id"]: w for w in hit_windows}
        hit_window_briefs = [
            _window_brief(window_index[wid], window_sources.get(wid, []))
            for wid in slice_row["hit_window_ids"]
        ]
        partition_index: dict[int, dict] = {}
        for brief in hit_window_briefs:
            for src in brief["source_partitions"]:
                partition_index[src["partition_id"]] = src
        slice_tasks.append(
            {
                "kind": "slice",
                "turbine_id": slice_row["turbine_id"],
                "slice_name": slice_row["slice_name"],
                "slice_id": slice_row["id"],
                "slice_version": slice_row["version"],
                "hit_windows": hit_window_briefs,
                "hit_window_ids": slice_row["hit_window_ids"],
                "source_partitions": sorted(
                    partition_index.values(), key=lambda x: x["utc_date"]
                ),
            }
        )
    return feature_tasks, slice_tasks


def impact_fingerprint(impact: dict) -> dict:
    """所见版本指纹：确认前后任一部分变化都说明预览已过期。"""
    partition_ids: set[int] = set()
    for window in impact["windows"]:
        for src in window["source_partitions"]:
            partition_ids.add(src["partition_id"])
    ref_pairs: set[tuple[int, int]] = set()
    for slice_row in impact["slices"]:
        for wid in slice_row["hit_window_ids"]:
            ref_pairs.add((slice_row["id"], wid))
    return {
        "window_ids": sorted(w["id"] for w in impact["windows"]),
        "slice_ids": sorted(s["id"] for s in impact["slices"]),
        "partition_ids": sorted(partition_ids),
        "slice_window_refs": sorted(list(ref_pairs)),
    }


# --------------------------------------------------------------------------- #
# 登记服务（版本化、不可变）
# --------------------------------------------------------------------------- #
def upload_partition(conn: sqlite3.Connection, payload: PartitionIn) -> dict:
    row = conn.execute(
        "SELECT MAX(revision) AS max_rev FROM partition_rev WHERE turbine_id = ? AND utc_date = ?",
        (payload.turbine_id, payload.utc_date),
    ).fetchone()
    max_rev = row["max_rev"]
    if max_rev is not None and payload.revision <= max_rev:
        raise ConflictError(
            f"风机 {payload.turbine_id} 在 {payload.utc_date} 的分区修订号必须严格递增，"
            f"当前最大为 {max_rev}，收到 {payload.revision}"
        )
    cur = conn.execute(
        "INSERT INTO partition_rev (turbine_id, utc_date, revision, created_at) "
        "VALUES (?, ?, ?, ?)",
        (payload.turbine_id, payload.utc_date, payload.revision, now_iso()),
    )
    return _get_row(conn, "partition_rev", cur.lastrowid)


def register_window(conn: sqlite3.Connection, payload: WindowIn) -> dict:
    parse_date(payload.end_date)
    row = conn.execute(
        "SELECT MAX(version) AS max_ver FROM feature_window "
        "WHERE turbine_id = ? AND feature_name = ? AND end_date = ?",
        (payload.turbine_id, payload.feature_name, payload.end_date),
    ).fetchone()
    max_ver = row["max_ver"]
    version = payload.version if payload.version is not None else (max_ver or 0) + 1
    if max_ver is not None and version <= max_ver:
        raise ConflictError(
            f"窗口 {payload.turbine_id}/{payload.feature_name}/{payload.end_date} "
            f"版本必须严格递增，当前最大为 {max_ver}，收到 {version}"
        )
    cur = conn.execute(
        "INSERT INTO feature_window (turbine_id, feature_name, end_date, version, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            payload.turbine_id,
            payload.feature_name,
            payload.end_date,
            version,
            now_iso(),
        ),
    )
    return _get_row(conn, "feature_window", cur.lastrowid)


def register_slice(conn: sqlite3.Connection, payload: SliceIn) -> dict:
    row = conn.execute(
        "SELECT MAX(version) AS max_ver FROM scoring_slice "
        "WHERE turbine_id = ? AND slice_name = ?",
        (payload.turbine_id, payload.slice_name),
    ).fetchone()
    max_ver = row["max_ver"]
    version = payload.version if payload.version is not None else (max_ver or 0) + 1
    if max_ver is not None and version <= max_ver:
        raise ConflictError(
            f"切片 {payload.turbine_id}/{payload.slice_name} 版本必须严格递增，"
            f"当前最大为 {max_ver}，收到 {version}"
        )

    # 校验引用的窗口版本全部存在且属于同一风机
    resolved: list[int] = []
    seen: set[int] = set()
    for ref in payload.window_refs:
        parse_date(ref.end_date)
        win = conn.execute(
            "SELECT * FROM feature_window WHERE turbine_id = ? AND feature_name = ? "
            "AND end_date = ? AND version = ?",
            (ref.turbine_id, ref.feature_name, ref.end_date, ref.version),
        ).fetchone()
        if win is None:
            raise BadRequestError(
                f"引用的窗口版本不存在: {ref.turbine_id}/{ref.feature_name}/"
                f"{ref.end_date}#v{ref.version}"
            )
        if win["turbine_id"] != payload.turbine_id:
            raise BadRequestError(
                f"切片 {payload.slice_name} 只能引用同风机 {payload.turbine_id} 的窗口，"
                f"不能引用风机 {win['turbine_id']} 的窗口"
            )
        if win["id"] not in seen:
            seen.add(win["id"])
            resolved.append(win["id"])

    cur = conn.execute(
        "INSERT INTO scoring_slice (turbine_id, slice_name, version, created_at) "
        "VALUES (?, ?, ?, ?)",
        (payload.turbine_id, payload.slice_name, version, now_iso()),
    )
    slice_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO slice_window_ref (slice_id, window_id) VALUES (?, ?)",
        [(slice_id, wid) for wid in resolved],
    )
    result = _get_row(conn, "scoring_slice", slice_id)
    result["window_ids"] = resolved
    return result


# --------------------------------------------------------------------------- #
# 预览（补算清单）与确认
# --------------------------------------------------------------------------- #
def create_preview(conn: sqlite3.Connection, corrections: list[CorrectionIn]) -> tuple[int, dict]:
    impact = compute_impact(conn, corrections)
    fingerprint = impact_fingerprint(impact)
    cur = conn.execute(
        "INSERT INTO preview (status, corrections_json, snapshot_json, created_at) "
        "VALUES ('open', ?, ?, ?)",
        (
            json.dumps(
                [c.model_dump() for c in corrections], ensure_ascii=False
            ),
            json.dumps(
                {"fingerprint": fingerprint, "impact": impact}, ensure_ascii=False
            ),
            now_iso(),
        ),
    )
    preview_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO order_partition_ref (preview_id, partition_id) VALUES (?, ?)",
        [(preview_id, pid) for pid in fingerprint["partition_ids"]],
    )
    conn.executemany(
        "INSERT INTO order_window_ref (preview_id, window_id) VALUES (?, ?)",
        [(preview_id, w["id"]) for w in impact["windows"]],
    )
    conn.executemany(
        "INSERT INTO order_window_partition (preview_id, window_id, partition_id) "
        "VALUES (?, ?, ?)",
        [
            (preview_id, w["id"], src["partition_id"])
            for w in impact["windows"]
            for src in w["source_partitions"]
        ],
    )
    conn.executemany(
        "INSERT INTO order_slice_ref (preview_id, slice_id) VALUES (?, ?)",
        [(preview_id, s["id"]) for s in impact["slices"]],
    )
    return preview_id, impact


def _normalize_fingerprint(fp: dict) -> dict:
    """JSON 往返后引用对由 tuple 退化为 list，统一转成可比较的 tuple 集合载体。"""
    return {
        "window_ids": list(fp["window_ids"]),
        "slice_ids": list(fp["slice_ids"]),
        "partition_ids": list(fp["partition_ids"]),
        "slice_window_refs": sorted(
            tuple(pair) for pair in fp["slice_window_refs"]
        ),
    }


def _fingerprint_diff(stored: dict, current: dict) -> dict:
    diff: dict[str, dict] = {}
    for key in ("window_ids", "slice_ids", "partition_ids"):
        old, new = set(stored[key]), set(current[key])
        if old != new:
            diff[key] = {
                "removed_from_preview": sorted(old - new),
                "newly_hit": sorted(new - old),
            }
    old_pairs = {tuple(p) for p in stored["slice_window_refs"]}
    new_pairs = {tuple(p) for p in current["slice_window_refs"]}
    if old_pairs != new_pairs:
        diff["slice_window_refs"] = {
            "removed_from_preview": sorted(
                list(map(list, old_pairs - new_pairs))
            ),
            "newly_hit": sorted(list(map(list, new_pairs - old_pairs))),
        }
    return diff


def confirm_preview(conn: sqlite3.Connection, preview_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM preview WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"预览单 {preview_id} 不存在")
    if row["status"] == "confirmed":
        raise ConflictError(
            f"预览单 {preview_id} 已确认，确认号 {row['confirmation_no']}"
        )

    corrections = [
        CorrectionIn(**item) for item in json.loads(row["corrections_json"])
    ]
    current = compute_impact(conn, corrections)
    stored_fp = _normalize_fingerprint(
        json.loads(row["snapshot_json"])["fingerprint"]
    )
    current_fp = _normalize_fingerprint(impact_fingerprint(current))
    if stored_fp != current_fp:
        raise ConflictError(
            "预览已过期：依赖的分区修订、窗口或切片版本发生变化，"
            f"差异={json.dumps(_fingerprint_diff(stored_fp, current_fp), ensure_ascii=False)}，"
            "请重新生成预览"
        )

    seq_row = conn.execute("INSERT INTO order_seq DEFAULT VALUES")
    seq = seq_row.lastrowid
    confirmation_no = f"CF-{datetime.now(timezone.utc):%Y%m%d}-{seq:06d}"
    conn.execute(
        "UPDATE preview SET status = 'confirmed', confirmation_no = ?, "
        "confirmed_at = ? WHERE id = ?",
        (confirmation_no, now_iso(), preview_id),
    )
    return {
        "preview_id": preview_id,
        "confirmation_no": confirmation_no,
        "impact": current,
    }


# --------------------------------------------------------------------------- #
# 按确认号 / 预览 id 打开单据：仅凭留存引用复算原清单
# --------------------------------------------------------------------------- #
def get_order(conn: sqlite3.Connection, preview_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM preview WHERE id = ?", (preview_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"预览单 {preview_id} 不存在")

    windows: list[dict] = []
    window_sources: dict[int, list[dict]] = defaultdict(list)
    window_rows = conn.execute(
        """
        SELECT fw.*
        FROM order_window_ref owr
        JOIN feature_window fw ON fw.id = owr.window_id
        WHERE owr.preview_id = ?
        ORDER BY fw.turbine_id, fw.feature_name, fw.end_date
        """,
        (preview_id,),
    ).fetchall()
    source_rows = conn.execute(
        """
        SELECT owp.window_id, pr.id AS partition_id, pr.utc_date, pr.revision,
               pr.turbine_id
        FROM order_window_partition owp
        JOIN partition_rev pr ON pr.id = owp.partition_id
        WHERE owp.preview_id = ?
        ORDER BY pr.utc_date
        """,
        (preview_id,),
    ).fetchall()
    for src in source_rows:
        window_sources[src["window_id"]].append(
            {
                "partition_id": src["partition_id"],
                "utc_date": src["utc_date"],
                "revision": src["revision"],
            }
        )
    for wr in window_rows:
        record = dict(wr)
        record["covered_dates"] = three_days(wr["end_date"])
        record["source_partitions"] = window_sources.get(wr["id"], [])
        windows.append(record)

    slice_rows = conn.execute(
        """
        SELECT ss.*
        FROM order_slice_ref osr
        JOIN scoring_slice ss ON ss.id = osr.slice_id
        WHERE osr.preview_id = ?
        ORDER BY ss.turbine_id, ss.slice_name
        """,
        (preview_id,),
    ).fetchall()
    pair_rows = conn.execute(
        """
        SELECT swr.slice_id, swr.window_id
        FROM slice_window_ref swr
        JOIN order_window_ref owr
          ON owr.window_id = swr.window_id AND owr.preview_id = ?
        WHERE swr.slice_id IN (
            SELECT slice_id FROM order_slice_ref WHERE preview_id = ?
        )
        ORDER BY swr.slice_id, swr.window_id
        """,
        (preview_id, preview_id),
    ).fetchall()
    pairs: dict[int, list[int]] = defaultdict(list)
    for pair in pair_rows:
        pairs[pair["slice_id"]].append(pair["window_id"])
    slices = []
    for sr in slice_rows:
        record = dict(sr)
        record["hit_window_ids"] = pairs.get(sr["id"], [])
        slices.append(record)

    feature_tasks, slice_tasks = build_tasks(windows, slices, window_sources)
    retained_partitions = [
        r["partition_id"]
        for r in conn.execute(
            "SELECT partition_id FROM order_partition_ref WHERE preview_id = ? "
            "ORDER BY partition_id",
            (preview_id,),
        ).fetchall()
    ]
    return {
        "preview_id": preview_id,
        "status": row["status"],
        "confirmation_no": row["confirmation_no"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "corrections": json.loads(row["corrections_json"]),
        "retained_refs": {
            "partition_ids": retained_partitions,
            "window_ids": sorted(w["id"] for w in windows),
            "slice_ids": sorted(s["id"] for s in slices),
        },
        "windows": windows,
        "slices": slices,
        "feature_tasks": feature_tasks,
        "slice_tasks": slice_tasks,
        "tasks": feature_tasks + slice_tasks,
        "rebuilt_from_retained_refs": True,
    }


def find_order_by_confirmation_no(
    conn: sqlite3.Connection, confirmation_no: str
) -> dict:
    row = conn.execute(
        "SELECT id FROM preview WHERE confirmation_no = ?", (confirmation_no,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"确认号 {confirmation_no} 不存在")
    return get_order(conn, row["id"])


# --------------------------------------------------------------------------- #
# 列表查询
# --------------------------------------------------------------------------- #
def _get_row(conn: sqlite3.Connection, table: str, row_id: int) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return dict(row)


def list_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def list_slices(conn: sqlite3.Connection) -> list[dict]:
    result = []
    for row in conn.execute("SELECT * FROM scoring_slice ORDER BY id").fetchall():
        record = dict(row)
        record["window_ids"] = [
            r["window_id"]
            for r in conn.execute(
                "SELECT window_id FROM slice_window_ref WHERE slice_id = ? ORDER BY window_id",
                (row["id"],),
            ).fetchall()
        ]
        result.append(record)
    return result


def list_previews(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, status, confirmation_no, created_at, confirmed_at, "
        "corrections_json FROM preview ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        record = dict(row)
        record["corrections"] = json.loads(record.pop("corrections_json"))
        result.append(record)
    return result

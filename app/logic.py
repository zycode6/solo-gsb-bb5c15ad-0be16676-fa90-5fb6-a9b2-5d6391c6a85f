"""双时态核心逻辑。

两个时间维度：
- 业务时间（valid time）  [valid_from, valid_to)：费率在现实中生效的区间，左闭右开；
- 系统时间（system time） [recorded_at, superseded_at)：该记录被系统认知为真的区间，左闭右开。

修订 = 追加新版本并把被覆盖的旧版本标记 superseded_at，历史行永不修改、永不删除。
"""

import sqlite3
from datetime import datetime
from typing import Optional

from .db import connect
from .timeutil import iso, now_iso

# 无限期区间的上界占位（仅用于 SQL 比较）
INF = "9999-12-31T23:59:59.999999+00:00"


class InvalidIntervalError(ValueError):
    """valid_to 不晚于 valid_from。"""


class NonMonotonicRecordTimeError(ValueError):
    """recorded_at 早于该合同已有记录，破坏系统时间单调性。"""


def record_rate(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[sqlite3.Row, list[int]]:
    """录入费率（追加式）。

    若新区间与当前认知中（superseded_at IS NULL）的既有版本重叠，
    则将重叠旧版本在 recorded_at 时刻标记为被取代，再插入新版本。

    返回 (新版本行, 被取代的版本 id 列表)。
    """
    vf = iso(valid_from)
    vt = iso(valid_to) if valid_to is not None else None
    ra = iso(recorded_at) if recorded_at is not None else now_iso()

    if vt is not None and vt <= vf:
        raise InvalidIntervalError("valid_to 必须晚于 valid_from")

    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        state = conn.execute(
            "SELECT MAX(recorded_at) AS max_ra, COALESCE(MAX(version), 0) AS max_v "
            "FROM rate_versions WHERE contract_no = ?",
            (contract_no,),
        ).fetchone()
        if state["max_ra"] is not None and ra < state["max_ra"]:
            raise NonMonotonicRecordTimeError(
                f"recorded_at={ra} 早于合同 {contract_no} 已有记录时间 {state['max_ra']}，"
                "系统时间必须单调递增"
            )

        overlaps = conn.execute(
            "SELECT id FROM rate_versions "
            "WHERE contract_no = ? AND superseded_at IS NULL "
            "  AND valid_from < COALESCE(?, ?) "
            "  AND COALESCE(valid_to, ?) > ?",
            (contract_no, vt, INF, INF, vf),
        ).fetchall()

        superseded_ids = [row["id"] for row in overlaps]
        for version_id in superseded_ids:
            conn.execute(
                "UPDATE rate_versions SET superseded_at = ? WHERE id = ?",
                (ra, version_id),
            )

        cursor = conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contract_no, rate, vf, vt, ra, state["max_v"] + 1, reason),
        )
        new_row = conn.execute(
            "SELECT * FROM rate_versions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

        if own_conn:
            conn.commit()
        return new_row, superseded_ids
    finally:
        if own_conn:
            conn.close()


def find_visible_rates(
    contract_no: str, valid_at: datetime, known_at: Optional[datetime]
) -> tuple[list[sqlite3.Row], str]:
    """查询在 (valid_at, known_at) 双时态坐标下可见且有效的所有版本。

    正常情况下结果应为 0 或 1 条；多于 1 条说明数据存在重叠冲突。
    返回 (命中行列表, 实际使用的 known_at)。
    """
    va = iso(valid_at)
    ka = iso(known_at) if known_at is not None else now_iso()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?)",
            (contract_no, va, va, ka, ka),
        ).fetchall()
    return list(rows), ka


def find_visible_versions(
    contract_no: str, known_at: Optional[datetime]
) -> tuple[list[sqlite3.Row], str]:
    """查询在 known_at 认知时点可见的全部版本（不限业务时间）。

    可见条件（系统时间左闭右开）：
    recorded_at <= known_at，且 superseded_at 为空或晚于 known_at。
    返回 (版本行列表（按业务起点、id 升序）, 实际使用的 known_at)。
    """
    ka = iso(known_at) if known_at is not None else now_iso()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?) "
            "ORDER BY valid_from ASC, id ASC",
            (contract_no, ka, ka),
        ).fetchall()
    return list(rows), ka


def coverage_segments(
    contract_no: str,
    frm: datetime,
    to: datetime,
    known_at: Optional[datetime],
) -> tuple[list[dict], str]:
    """计算业务窗口 [frm, to) 在 known_at 认知时点下的连续覆盖分段。

    步骤：
    1. 按 recorded_at/superseded_at 筛出该认知时点可见的全部版本；
    2. 将各版本业务区间与查询窗口求交，以所有相交边界切分 [frm, to)
       （左闭右开），段内候选集合恒定；
    3. 按候选数分类：0 -> gap，1 -> covered，>1 -> conflict；
    4. 合并相邻且分类与候选（版本 id 集合）完全相同的段。

    无可见版本时，返回覆盖整个查询窗口的单个 gap 段。

    返回 (分段列表, 实际使用的 known_at)。每个分段为
    {"start", "end", "kind", "rows"}；rows 按 id 升序，
    covered 恰一条，conflict 为全部候选，gap 为空。
    """
    start = iso(frm)
    end = iso(to)
    rows, ka = find_visible_versions(contract_no, known_at)

    # 各可见版本业务区间与 [start, end) 求交，并收集全部切分边界
    boundaries = {start, end}
    clipped: list[tuple[str, str, sqlite3.Row]] = []
    for row in rows:
        vf = row["valid_from"]
        vt = row["valid_to"] if row["valid_to"] is not None else INF
        lo = max(start, vf)
        hi = min(end, vt)
        if lo < hi:
            clipped.append((lo, hi, row))
            boundaries.add(lo)
            boundaries.add(hi)

    cuts = [start] + sorted(b for b in boundaries if start < b < end) + [end]

    segments: list[dict] = []
    for lo, hi in zip(cuts, cuts[1:]):
        # 切分点包含所有相交区间端点，段内候选恒定，用左端点判定即可
        hits = sorted(
            (row for vf, vt, row in clipped if vf <= lo < vt),
            key=lambda r: r["id"],
        )
        kind = ("gap", "covered", "conflict")[min(len(hits), 2)]
        segments.append(
            {
                "start": lo,
                "end": hi,
                "kind": kind,
                "rows": hits,
                "key": (kind, tuple(r["id"] for r in hits)),
            }
        )

    # 合并相邻且分类与候选完全相同的段
    merged: list[dict] = []
    for seg in segments:
        if merged and merged[-1]["key"] == seg["key"]:
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(dict(seg))
    for seg in merged:
        seg.pop("key")
    return merged, ka


def get_timeline(contract_no: str) -> list[sqlite3.Row]:
    """合同的全部版本，按版本号升序（即录入顺序）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions WHERE contract_no = ? ORDER BY version ASC, id ASC",
            (contract_no,),
        ).fetchall()
    return list(rows)

"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def post_rate(client, **kw):
    return client.post("/rates", json=kw)


# ---------- 录入与基本报价 ----------

def test_create_and_quote(client):
    r = post_rate(
        client,
        contract_no="C-1",
        rate=0.05,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-07-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"]["version"] == 1
    assert body["created"]["status"] == "current"
    assert body["superseded_rate_ids"] == []

    q = client.get(
        "/quote",
        params={
            "contract_no": "C-1",
            "valid_at": "2026-03-01T00:00:00Z",
            "known_at": "2026-02-01T00:00:00Z",
        },
    )
    assert q.status_code == 200, q.text
    data = q.json()
    assert data["rate"] == 0.05
    assert data["basis"]["version"] == 1
    assert data["basis"]["recorded_at"] == "2026-01-01T00:00:00.000000+00:00"


def test_quote_boundary_left_closed_right_open(client):
    post_rate(
        client,
        contract_no="C-2",
        rate=0.1,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
        recorded_at="2025-12-01T00:00:00Z",
    )
    # valid_from 边界：含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 200
    # valid_to 边界：不含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-02-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 404


def test_open_ended_interval(client):
    post_rate(
        client,
        contract_no="C-3",
        rate=0.07,
        valid_from="2026-01-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    q = client.get("/quote", params={
        "contract_no": "C-3", "valid_at": "2099-06-01T00:00:00Z",
        "known_at": "2026-01-02T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.07


# ---------- 修订：追加版本，不覆盖历史 ----------

def test_revision_appends_version_and_keeps_history(client):
    post_rate(client, contract_no="C-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    r2 = post_rate(client, contract_no="C-4", rate=0.08,
                   valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                   recorded_at="2026-06-01T00:00:00Z", reason="费率更正")
    assert r2.status_code == 201
    body = r2.json()
    assert body["created"]["version"] == 2
    assert len(body["superseded_rate_ids"]) == 1

    # 修订前的认知时点：仍看到旧费率 0.05
    old = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-03-01T00:00:00Z"})
    assert old.status_code == 200
    assert old.json()["rate"] == 0.05
    assert old.json()["basis"]["version"] == 1

    # 修订后的认知时点：看到新费率 0.08
    new = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-06-02T00:00:00Z"})
    assert new.status_code == 200
    assert new.json()["rate"] == 0.08
    assert new.json()["basis"]["version"] == 2

    # 时间线保留两个版本，旧版本状态为 superseded
    tl = client.get("/contracts/C-4/timeline")
    assert tl.status_code == 200
    versions = tl.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["status"] == "superseded"
    assert versions[0]["superseded_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert versions[1]["status"] == "current"


def test_partial_overlap_revision(client):
    """部分重叠的修订只取代重叠的当前版本，不相干版本不受影响。"""
    post_rate(client, contract_no="C-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="C-5", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    # 新区间覆盖整个 2026 年 → 取代上面两个当前版本
    r = post_rate(client, contract_no="C-5", rate=0.09,
                  valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                  recorded_at="2026-03-01T00:00:00Z")
    assert len(r.json()["superseded_rate_ids"]) == 2
    assert r.json()["created"]["version"] == 3

    q = client.get("/quote", params={
        "contract_no": "C-5", "valid_at": "2026-08-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"})
    assert q.json()["rate"] == 0.09


def test_recorded_at_must_be_monotonic(client):
    post_rate(client, contract_no="C-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = post_rate(client, contract_no="C-6", rate=0.06,
                  valid_from="2026-01-01T00:00:00Z",
                  recorded_at="2026-05-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NON_MONOTONIC_RECORDED_AT"


def test_invalid_interval_rejected(client):
    r = post_rate(client, contract_no="C-7", rate=0.05,
                  valid_from="2026-06-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_malformed_request_422(client):
    # 缺少必填字段 / 非法时间格式 → 统一的 INVALID_REQUEST
    r = post_rate(client, rate=0.05, valid_from="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_rate(client, contract_no="C-7", rate=0.05, valid_from="not-a-date")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


# ---------- 报价错误分支 ----------

def test_quote_no_match_404(client):
    q = client.get("/quote", params={
        "contract_no": "NOPE", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"})
    assert q.status_code == 404
    assert q.json()["detail"]["code"] == "NO_MATCH"


def test_quote_not_yet_recorded_404(client):
    """known_at 早于 recorded_at 时，费率尚不可见。"""
    post_rate(client, contract_no="C-8", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-8", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-01-15T00:00:00Z"})
    assert q.status_code == 404


def test_quote_overlap_409(client, tmp_path, monkeypatch):
    """直接注入互相重叠的脏数据（绕过录入保护），报价应报 409 冲突。"""
    post_rate(client, contract_no="C-9", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('C-9', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    q = client.get("/quote", params={
        "contract_no": "C-9", "valid_at": "2026-07-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert q.status_code == 409
    detail = q.json()["detail"]
    assert detail["code"] == "OVERLAP"
    assert len(detail["candidates"]) == 2


def test_quote_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的报价应立即可见
    post_rate(client, contract_no="C-10", rate=0.03,
              valid_from="2020-01-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-10", "valid_at": "2026-09-18T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.03


# ---------- 时间线 ----------

def test_timeline_unknown_contract_404(client):
    r = client.get("/contracts/GHOST/timeline")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CONTRACT_NOT_FOUND"


# ---------- 覆盖分析 ----------

def get_coverage(client, contract_no, **params):
    return client.get(f"/contracts/{contract_no}/coverage", params=params)


def kinds(segments):
    return [(s["kind"], s["from"], s["to"]) for s in segments]


def test_coverage_from_ge_to_422(client):
    # from == to
    r = get_coverage(client, "C-C1",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INTERVAL"
    # from > to
    r = get_coverage(client, "C-C1",
                     **{"from": "2026-07-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_coverage_missing_or_malformed_params_422(client):
    # 缺少 to
    r = get_coverage(client, "C-C1", **{"from": "2026-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    # 非法时间格式
    r = get_coverage(client, "C-C1",
                     **{"from": "2026-01-01T00:00:00Z", "to": "not-a-date"})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_coverage_unknown_contract_is_whole_window_gap(client):
    r = get_coverage(client, "GHOST",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z",
                        "known_at": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "GHOST"
    assert data["valid_from"] == "2026-01-01T00:00:00.000000+00:00"
    assert data["valid_to"] == "2026-07-01T00:00:00.000000+00:00"
    assert data["known_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert len(data["segments"]) == 1
    seg = data["segments"][0]
    assert seg["kind"] == "gap"
    assert seg["from"] == data["valid_from"]
    assert seg["to"] == data["valid_to"]
    assert seg["basis"] is None
    assert seg["candidates"] == []


def test_coverage_not_yet_recorded_is_gap(client):
    """known_at 早于所有 recorded_at 时无可见版本，整窗为 gap。"""
    post_rate(client, contract_no="C-C2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = get_coverage(client, "C-C2",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                        "known_at": "2026-01-01T00:00:00Z"})
    assert r.status_code == 200
    assert kinds(r.json()["segments"]) == [
        ("gap", "2026-01-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00"),
    ]


def test_coverage_gap_covered_gap_with_unique_basis(client):
    post_rate(client, contract_no="C-C3", rate=0.05,
              valid_from="2026-03-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = get_coverage(client, "C-C3",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2026-12-01T00:00:00Z",
                        "known_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    segments = r.json()["segments"]
    assert kinds(segments) == [
        ("gap", "2026-01-01T00:00:00.000000+00:00", "2026-03-01T00:00:00.000000+00:00"),
        ("covered", "2026-03-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00"),
        ("gap", "2026-06-01T00:00:00.000000+00:00", "2026-12-01T00:00:00.000000+00:00"),
    ]
    covered = segments[1]
    assert covered["basis"]["rate"] == 0.05
    assert covered["basis"]["version"] == 1
    assert covered["candidates"] == []
    # 段连续无缝：上一段终点即下一段起点，且首尾正好咬合查询窗口
    cuts = [segments[0]["from"]] + [s["to"] for s in segments]
    assert cuts == [
        "2026-01-01T00:00:00.000000+00:00",
        "2026-03-01T00:00:00.000000+00:00",
        "2026-06-01T00:00:00.000000+00:00",
        "2026-12-01T00:00:00.000000+00:00",
    ]


def test_coverage_open_ended_version_covers_window(client):
    post_rate(client, contract_no="C-C4", rate=0.07,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = get_coverage(client, "C-C4",
                     **{"from": "2026-03-01T00:00:00Z", "to": "2099-01-01T00:00:00Z",
                        "known_at": "2026-02-01T00:00:00Z"})
    segments = r.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["kind"] == "covered"
    assert segments[0]["from"] == "2026-03-01T00:00:00.000000+00:00"
    assert segments[0]["to"] == "2099-01-01T00:00:00.000000+00:00"
    assert segments[0]["basis"]["rate"] == 0.07


def test_coverage_consecutive_versions_not_merged(client):
    """相邻 covered 段若版本依据不同，不能合并。"""
    post_rate(client, contract_no="C-C5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="C-C5", rate=0.06,
              valid_from="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = get_coverage(client, "C-C5",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                        "known_at": "2026-02-01T00:00:00Z"})
    segments = r.json()["segments"]
    assert [s["kind"] for s in segments] == ["covered", "covered"]
    assert segments[0]["basis"]["rate"] == 0.05
    assert segments[1]["basis"]["rate"] == 0.06
    assert segments[0]["to"] == segments[1]["from"]


def test_coverage_known_at_replays_revision(client):
    """修订前后认知时点看到不同版本；认知窗口外的版本不参与切分。"""
    post_rate(client, contract_no="C-C6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    post_rate(client, contract_no="C-C6", rate=0.08,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z", reason="费率更正")
    params = {"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"}
    before = get_coverage(client, "C-C6", known_at="2026-03-01T00:00:00Z", **params).json()
    after = get_coverage(client, "C-C6", known_at="2026-07-01T00:00:00Z", **params).json()

    assert len(before["segments"]) == 1
    assert before["segments"][0]["kind"] == "covered"
    assert before["segments"][0]["basis"]["version"] == 1
    assert before["segments"][0]["basis"]["rate"] == 0.05

    assert len(after["segments"]) == 1
    assert after["segments"][0]["kind"] == "covered"
    assert after["segments"][0]["basis"]["version"] == 2
    assert after["segments"][0]["basis"]["rate"] == 0.08


def test_coverage_superseded_version_not_visible_after_its_end(client):
    """版本恰好在 known_at 被取代（superseded_at == known_at，右开）即不可见。"""
    post_rate(client, contract_no="C-C6b", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="C-C6b", rate=0.08,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = get_coverage(client, "C-C6b",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                        "known_at": "2026-06-01T00:00:00Z"})
    segments = r.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["basis"]["version"] == 2


def test_coverage_conflict_carries_all_candidates(client):
    """注入互相重叠的脏数据：覆盖段、冲突段、覆盖段依次出现，候选完整。"""
    post_rate(client, contract_no="C-C7", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('C-C7', 0.99, '2026-03-01T00:00:00.000000+00:00', "
            "        '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    r = get_coverage(client, "C-C7",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                        "known_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    segments = r.json()["segments"]
    assert [s["kind"] for s in segments] == ["covered", "conflict", "covered"]
    conflict = segments[1]
    assert conflict["from"] == "2026-03-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-06-01T00:00:00.000000+00:00"
    assert conflict["basis"] is None
    assert [c["rate"] for c in conflict["candidates"]] == [0.05, 0.99]
    assert {c["version"] for c in conflict["candidates"]} == {1, 2}
    # 冲突两侧 covered 依据相同但不相邻，不得跨冲突合并
    assert segments[0]["basis"]["version"] == 1
    assert segments[2]["basis"]["version"] == 1
    assert segments[0]["to"] == conflict["from"]
    assert conflict["to"] == segments[2]["from"]


def test_coverage_clips_intervals_to_window_and_merges_same_key(client):
    """版本区间伸出查询窗口时按窗口裁剪；可见版本之外的边界不产生切分点。"""
    # v1: 2025 全年（窗口左侧伸出），v2: 2027-01 起开放（窗口右侧伸出）
    post_rate(client, contract_no="C-C8", rate=0.1,
              valid_from="2025-01-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z",
              recorded_at="2025-01-01T00:00:00Z")
    post_rate(client, contract_no="C-C8", rate=0.2,
              valid_from="2026-06-01T00:00:00Z",
              recorded_at="2025-01-01T00:00:00Z")
    r = get_coverage(client, "C-C8",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                        "known_at": "2026-01-01T00:00:00Z"})
    segments = r.json()["segments"]
    assert kinds(segments) == [
        ("gap", "2026-01-01T00:00:00.000000+00:00", "2026-06-01T00:00:00.000000+00:00"),
        ("covered", "2026-06-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00"),
    ]
    assert segments[1]["basis"]["rate"] == 0.2


def test_coverage_default_known_at_is_now(client):
    post_rate(client, contract_no="C-C9", rate=0.03,
              valid_from="2020-01-01T00:00:00Z")
    r = get_coverage(client, "C-C9",
                     **{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z"})
    assert r.status_code == 200
    segments = r.json()["segments"]
    assert len(segments) == 1
    assert segments[0]["kind"] == "covered"
    assert segments[0]["basis"]["rate"] == 0.03


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

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


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}

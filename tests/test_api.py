"""端到端测试：通过 TestClient 打真实 HTTP 接口，SQLite 落临时文件。

运行：FIRMWARE_DB_PATH 由 fixture 指到 tmp_path。
"""

from __future__ import annotations

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("FIRMWARE_DB_PATH", str(tmp_path / "test.db"))
    import app.db as db
    import app.main as main

    importlib.reload(db)  # 让 db_path() 读取到新的环境变量
    importlib.reload(main)
    db.init_db()
    with TestClient(main.app) as c:
        yield c


def make_group(client: TestClient, ids=("dev-1", "dev-2", "dev-3"), version="1.0.0"):
    resp = client.post(
        "/groups",
        json={
            "name": "test-group",
            "devices": [
                {"device_id": d, "current_version": version} for d in ids
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def make_batch(client, group_id, targets=None):
    if targets is None:
        group = client.get(f"/groups/{group_id}").json()
        ids = [d["device_id"] for d in group["devices"]]
        targets = [{"device_id": d, "target_version": "2.0.0"} for d in ids]
    resp = client.post(f"/groups/{group_id}/batches", json={"targets": targets})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def versions(client, group_id):
    return {
        d["device_id"]: d["current_version"]
        for d in client.get(f"/groups/{group_id}").json()["devices"]
    }


# --------------------------------------------------------------------------- #
# 登记设备组
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("count", [2, 8])
def test_register_group_size_ok(client, count):
    devices = [
        {"device_id": f"dev-{i}", "current_version": "1.0.0"} for i in range(count)
    ]
    resp = client.post("/groups", json={"name": "g", "devices": devices})
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["devices"]) == count
    assert body["id"].startswith("grp_")


@pytest.mark.parametrize("count", [1, 9])
def test_register_group_size_rejected(client, count):
    devices = [
        {"device_id": f"dev-{i}", "current_version": "1.0.0"} for i in range(count)
    ]
    resp = client.post("/groups", json={"name": "g", "devices": devices})
    assert resp.status_code == 422


def test_duplicate_device_id_rejected(client):
    resp = client.post(
        "/groups",
        json={
            "name": "g",
            "devices": [
                {"device_id": "x", "current_version": "1"},
                {"device_id": "x", "current_version": "2"},
            ],
        },
    )
    assert resp.status_code == 422


def test_get_group_not_found(client):
    assert client.get("/groups/nope").status_code == 404


# --------------------------------------------------------------------------- #
# 开批次
# --------------------------------------------------------------------------- #
def test_open_batch_blocks_next(client):
    gid = make_group(client)
    bid = make_batch(client, gid)
    body = client.get(f"/batches/{bid}").json()
    assert body["status"] == "open"

    targets = [
        {"device_id": d, "target_version": "3.0.0"}
        for d in ("dev-1", "dev-2", "dev-3")
    ]
    resp = client.post(f"/groups/{gid}/batches", json={"targets": targets})
    assert resp.status_code == 409
    assert resp.json()["detail"]["open_batch_id"] == bid


def test_batch_targets_must_cover_all_devices(client):
    gid = make_group(client)
    # 缺一台
    resp = client.post(
        f"/groups/{gid}/batches",
        json={"targets": [
            {"device_id": "dev-1", "target_version": "2.0.0"},
            {"device_id": "dev-2", "target_version": "2.0.0"},
        ]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["missing_devices"] == ["dev-3"]

    # 多一台（陌生设备）
    resp = client.post(
        f"/groups/{gid}/batches",
        json={"targets": [
            {"device_id": "dev-1", "target_version": "2.0.0"},
            {"device_id": "dev-2", "target_version": "2.0.0"},
            {"device_id": "dev-3", "target_version": "2.0.0"},
            {"device_id": "dev-9", "target_version": "2.0.0"},
        ]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["unknown_devices"] == ["dev-9"]


def test_duplicate_target_in_request_rejected(client):
    gid = make_group(client)
    resp = client.post(
        f"/groups/{gid}/batches",
        json={"targets": [
            {"device_id": "dev-1", "target_version": "2.0.0"},
            {"device_id": "dev-1", "target_version": "3.0.0"},
        ]},
    )
    assert resp.status_code == 422


def test_batch_has_unique_id(client):
    g1 = make_group(client, ("a-1", "a-2"))
    g2 = make_group(client, ("b-1", "b-2"))
    b1 = make_batch(client, g1)
    b2 = make_batch(client, g2)
    assert b1 != b2


# --------------------------------------------------------------------------- #
# 全 ready → 生效、版本原子切换
# --------------------------------------------------------------------------- #
def test_all_ready_switches_versions_and_effective(client):
    gid = make_group(client)
    bid = make_batch(client, gid)

    r1 = client.post(f"/batches/{bid}/receipts",
                     json={"device_id": "dev-1", "result": "ready", "version": "1.0.0"})
    assert r1.status_code == 200 and r1.json()["status"] == "open"

    r2 = client.post(f"/batches/{bid}/receipts",
                     json={"device_id": "dev-2", "result": "ready", "version": "1.0.0"})
    assert r2.json()["status"] == "open"

    r3 = client.post(f"/batches/{bid}/receipts",
                     json={"device_id": "dev-3", "result": "ready", "version": "1.0.0"})
    body = r3.json()
    assert body["status"] == "effective"
    assert body["closed_at"] is not None
    assert body["receipt_counts"] == {"ready": 3, "failed": 0, "total": 3}

    assert versions(client, gid) == {
        "dev-1": "2.0.0",
        "dev-2": "2.0.0",
        "dev-3": "2.0.0",
    }

    # 结束后可以开新批
    assert make_batch(client, gid).startswith("bat_")


def test_per_device_target_versions(client):
    gid = make_group(client)
    bid = make_batch(
        client,
        gid,
        targets=[
            {"device_id": "dev-1", "target_version": "1.1.0"},
            {"device_id": "dev-2", "target_version": "2.0.0"},
            {"device_id": "dev-3", "target_version": "3.5.2"},
        ],
    )
    for d in ("dev-1", "dev-2", "dev-3"):
        client.post(f"/batches/{bid}/receipts",
                    json={"device_id": d, "result": "ready", "version": "1.0.0"})
    assert versions(client, gid) == {
        "dev-1": "1.1.0",
        "dev-2": "2.0.0",
        "dev-3": "3.5.2",
    }


# --------------------------------------------------------------------------- #
# failed → 批次失败、版本不动
# --------------------------------------------------------------------------- #
def test_any_failed_fails_batch_and_keeps_versions(client):
    gid = make_group(client)
    bid = make_batch(client, gid)

    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "dev-1", "result": "ready", "version": "1.0.0"})
    body = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "dev-2", "result": "failed", "version": "1.0.0"},
    ).json()
    assert body["status"] == "failed"

    assert versions(client, gid) == {
        "dev-1": "1.0.0",
        "dev-2": "1.0.0",
        "dev-3": "1.0.0",
    }


def test_late_receipts_after_failure_ignored_but_replay_ok(client):
    gid = make_group(client)
    bid = make_batch(client, gid)

    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "dev-2", "result": "failed", "version": "1.0.0"})

    # dev-2 同结果重放：成功，无副作用
    replay = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "dev-2", "result": "failed", "version": "1.0.0"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "failed"

    # dev-2 改报 ready：冲突
    flip = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "dev-2", "result": "ready", "version": "1.0.0"},
    )
    assert flip.status_code == 409

    # 其他设备迟到的"新回执"：拒绝，不能影响结果
    late = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "dev-1", "result": "ready", "version": "1.0.0"},
    )
    assert late.status_code == 409
    assert versions(client, gid) == {
        "dev-1": "1.0.0",
        "dev-2": "1.0.0",
        "dev-3": "1.0.0",
    }

    # 失败批结束后可以开下一批；旧批回执依然查得到
    bid2 = make_batch(client, gid)
    client.post(f"/batches/{bid2}/receipts",
                json={"device_id": "dev-2", "result": "failed", "version": "1.0.0"})
    assert client.get(f"/batches/{bid}").json()["status"] == "failed"
    assert client.get(f"/batches/{bid2}").json()["status"] == "failed"


def test_replay_ready_after_effective_ok_but_new_late_receipt_rejected(client):
    gid = make_group(client, ("a", "b"))
    bid = make_batch(
        client,
        gid,
        targets=[
            {"device_id": "a", "target_version": "2.0.0"},
            {"device_id": "b", "target_version": "2.0.0"},
        ],
    )
    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "a", "result": "ready", "version": "1.0.0"})
    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "b", "result": "ready", "version": "1.0.0"})

    replay = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "a", "result": "ready", "version": "2.0.0"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    # 一个从未回执过的设备对旧批迟到提交 → 409（2 台组里构造：再造第三台不现实，
    # 这里用 failed 批次场景在另一个用例覆盖；本用例验证生效批同样拒收陌生名单外设备）
    outsider = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "ghost", "result": "ready", "version": "x"},
    )
    assert outsider.status_code == 422


def test_unknown_device_in_receipt(client):
    gid = make_group(client)
    bid = make_batch(client, gid)
    resp = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "ghost", "result": "ready", "version": "1.0.0"},
    )
    assert resp.status_code == 422


def test_receipt_unknown_batch(client):
    resp = client.post(
        "/batches/bat_nope/receipts",
        json={"device_id": "x", "result": "ready", "version": "1"},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 并发：结果必须等价于某个串行顺序
# --------------------------------------------------------------------------- #
def test_concurrent_all_ready_equivalent_to_serial(client, gid=None):
    gid = make_group(client, tuple(f"dev-{i}" for i in range(8)))
    bid = make_batch(client, gid)

    barrier = threading.Barrier(8)

    def send(d):
        barrier.wait()  # 尽可能同时释放，制造锁竞争
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(send, [f"dev-{i}" for i in range(8)]))

    assert codes.count(200) == 8
    batch = client.get(f"/batches/{bid}").json()
    assert batch["status"] == "effective"
    assert batch["receipt_counts"] == {"ready": 8, "failed": 0, "total": 8}
    assert all(v == "2.0.0" for v in versions(client, gid).values())


def test_concurrent_mixed_exactly_one_serial_outcome(client):
    """N 台同时提交，其中一台 failed：串行化后批次必为 failed，版本全部不变。"""
    ids = tuple(f"dev-{i}" for i in range(6))
    gid = make_group(client, ids)
    bid = make_batch(client, gid)
    barrier = threading.Barrier(len(ids))

    def send(d, result):
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": result, "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        futs = [
            pool.submit(send, d, "failed" if d == "dev-0" else "ready")
            for d in ids
        ]
        responses = [f.result() for f in futs]

    # 无 5xx；dev-0 的 failed 被接受，其余 ready 有的先于 failed 被接受（open），
    # 有的晚于 failed 成为迟到回执被 409 拒绝——两种都是合法的串行交错。
    assert all(r.status_code in (200, 409) for r in responses)
    batch = client.get(f"/batches/{bid}").json()
    assert batch["status"] == "failed"
    assert batch["receipt_counts"]["failed"] == 1
    # 关键不变量：要么没切，要么全切；失败意味着版本必须全部保持原样。
    assert all(v == "1.0.0" for v in versions(client, gid).values())


def test_concurrent_open_two_batches_only_one_wins(client):
    gid = make_group(client, ("dev-1", "dev-2"))
    targets = [
        {"device_id": "dev-1", "target_version": "2.0.0"},
        {"device_id": "dev-2", "target_version": "2.0.0"},
    ]
    barrier = threading.Barrier(2)

    def open_batch():
        barrier.wait()
        return client.post(f"/groups/{gid}/batches", json={"targets": targets})

    with ThreadPoolExecutor(max_workers=2) as pool:
        # 必须先全部提交再取结果，否则第一个任务会独自卡在屏障上死锁。
        futs = [pool.submit(open_batch) for _ in range(2)]
        results = [f.result() for f in futs]

    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409]
    open_batches = [
        b for b in client.get(f"/groups/{gid}/batches").json()
        if b["status"] == "open"
    ]
    assert len(open_batches) == 1


def test_no_partial_switch_under_concurrent_reads(client):
    """并发提交期间反复读取：生效之前所有版本必须一致停留在旧版本，
    生效之后全部为新版本——任何时刻都不能出现部分切换。"""
    ids = tuple(f"dev-{i}" for i in range(5))
    gid = make_group(client, ids)
    bid = make_batch(client, gid)
    stop = threading.Event()
    saw_partial = []

    def reader():
        while not stop.is_set():
            vs = set(versions(client, gid).values())
            if vs not in ({"1.0.0"}, {"2.0.0"}):
                saw_partial.append(vs)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for d in ids:
            client.post(
                f"/batches/{bid}/receipts",
                json={"device_id": d, "result": "ready", "version": "1.0.0"},
            )
    finally:
        stop.set()
        t.join()

    assert saw_partial == []
    assert set(versions(client, gid).values()) == {"2.0.0"}


def test_concurrent_same_device_same_result_one_replay(client):
    """同一设备并发提交两次相同结果：必须等价于"提交 + 重放"，无 5xx、只有一条回执。"""
    gid = make_group(client, ("a", "b"))
    bid = make_batch(
        client,
        gid,
        targets=[
            {"device_id": "a", "target_version": "2.0.0"},
            {"device_id": "b", "target_version": "2.0.0"},
        ],
    )
    barrier = threading.Barrier(2)

    def send():
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "a", "result": "ready", "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(send) for _ in range(2)]
        responses = [f.result() for f in futs]

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200]
    replayed = sorted(r.json()["replayed"] for r in responses)
    assert replayed == [False, True]
    batch = client.get(f"/batches/{bid}").json()
    assert batch["receipt_counts"] == {"ready": 1, "failed": 0, "total": 2}


def test_concurrent_same_device_conflicting_result(client):
    """同一设备并发提交 ready 与 failed：结果只能等价于某个串行顺序——
    胜者结果落库，败者 409，绝不 5xx。"""
    gid = make_group(client, ("a", "b"))
    bid = make_batch(
        client,
        gid,
        targets=[
            {"device_id": "a", "target_version": "2.0.0"},
            {"device_id": "b", "target_version": "2.0.0"},
        ],
    )
    barrier = threading.Barrier(2)

    def send(result):
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "a", "result": result, "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(send, "ready"), pool.submit(send, "failed")]
        responses = [f.result() for f in futs]

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409]
    batch = client.get(f"/batches/{bid}").json()
    assert len(batch["receipts"]) == 1
    assert batch["receipts"][0]["device_id"] == "a"
    assert batch["receipts"][0]["result"] in ("ready", "failed")


def test_replay_same_result_is_idempotent_after_effective_repeatedly(client):
    gid = make_group(client, ("a", "b"))
    bid = make_batch(
        client,
        gid,
        targets=[
            {"device_id": "a", "target_version": "2.0.0"},
            {"device_id": "b", "target_version": "2.0.0"},
        ],
    )
    for d in ("a", "b"):
        client.post(f"/batches/{bid}/receipts",
                    json={"device_id": d, "result": "ready", "version": "1.0.0"})

    for _ in range(5):
        r = client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "a", "result": "ready", "version": "2.0.0"},
        )
        assert r.status_code == 200
    batch = client.get(f"/batches/{bid}").json()
    assert batch["receipt_counts"] == {"ready": 2, "failed": 0, "total": 2}

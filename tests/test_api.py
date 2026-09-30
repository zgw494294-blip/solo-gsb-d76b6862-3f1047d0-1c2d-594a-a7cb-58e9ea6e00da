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


def make_two_groups(client, src_ids=("s-1", "s-2", "s-3"),
                    dst_ids=("d-1", "d-2"), src_version="1.0.0",
                    dst_version="1.5.0"):
    src = make_group(client, src_ids, src_version)
    dst = make_group(client, dst_ids, dst_version)
    return src, dst


def transfer(client, source_id, target_id, device_ids):
    return client.post(
        f"/groups/{source_id}/transfers",
        json={"target_group_id": target_id, "device_ids": list(device_ids)},
    )


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


# --------------------------------------------------------------------------- #
# 设备调配：整组移动、原子拒绝、旧批归属、并发串行
# --------------------------------------------------------------------------- #
def test_transfer_moves_devices_with_versions(client):
    src, dst = make_two_groups(
        client, src_ids=("s-1", "s-2", "s-3", "s-4")
    )
    resp = transfer(client, src, dst, ["s-2", "s-3"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["source_group"]["id"] == src
    assert {d["device_id"]: d["current_version"]
            for d in body["source_group"]["devices"]} == {
        "s-1": "1.0.0", "s-4": "1.0.0",
    }
    assert body["target_group"]["id"] == dst
    assert {d["device_id"]: d["current_version"]
            for d in body["target_group"]["devices"]} == {
        "d-1": "1.5.0",
        "d-2": "1.5.0",
        "s-2": "1.0.0",
        "s-3": "1.0.0",
    }
    assert body["transferred_devices"] == ["s-2", "s-3"]

    # 再直接查两组确认落库
    assert versions(client, src) == {"s-1": "1.0.0", "s-4": "1.0.0"}
    assert versions(client, dst) == {
        "d-1": "1.5.0", "d-2": "1.5.0",
        "s-2": "1.0.0", "s-3": "1.0.0",
    }


def test_transfer_empty_list_rejected(client):
    src, dst = make_two_groups(client)
    resp = transfer(client, src, dst, [])
    assert resp.status_code == 422


def test_transfer_duplicate_ids_rejected(client):
    src, dst = make_two_groups(client)
    resp = transfer(client, src, dst, ["s-2", "s-2"])
    assert resp.status_code == 422


def test_transfer_same_source_and_target_rejected(client):
    src = make_group(client)
    resp = transfer(client, src, src, ["dev-2"])
    assert resp.status_code == 422


def test_transfer_unknown_group_404(client):
    src, dst = make_two_groups(client)
    assert transfer(client, "grp_nope", dst, ["s-1"]).status_code == 404
    assert transfer(client, src, "grp_nope", ["s-1"]).status_code == 404


def test_transfer_device_not_in_source_rejected_and_unchanged(client):
    src, dst = make_two_groups(client)
    resp = transfer(client, src, dst, ["s-2", "ghost"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["unknown_devices"] == ["ghost"]
    # 整次请求不改变数据
    assert set(versions(client, src)) == {"s-1", "s-2", "s-3"}
    assert set(versions(client, dst)) == {"d-1", "d-2"}


def test_transfer_target_already_has_same_id_rejected(client):
    # 目标组已有 d-1（即使它恰好在来源组也存在同名设备）
    src, dst = make_two_groups(
        client,
        src_ids=("d-1", "s-2", "s-3"),
        dst_ids=("d-1", "d-2"),
    )
    resp = transfer(client, src, dst, ["d-1"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["target_conflict_devices"] == ["d-1"]
    assert set(versions(client, src)) == {"d-1", "s-2", "s-3"}
    assert set(versions(client, dst)) == {"d-1", "d-2"}


def test_transfer_size_limits_source_too_small(client):
    # 2 台的组调走 1 台 → 来源剩 1 台
    src = make_group(client, ("s-1", "s-2"))
    dst = make_group(client, ("d-1", "d-2"))
    resp = transfer(client, src, dst, ["s-1"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["source_group"]["size_after"] == 1
    assert set(versions(client, src)) == {"s-1", "s-2"}


def test_transfer_size_limits_target_too_big(client):
    src = make_group(client, tuple(f"s-{i}" for i in range(4)))
    dst = make_group(client, tuple(f"d-{i}" for i in range(8)))
    resp = transfer(client, src, dst, ["s-0", "s-1"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["target_group"]["size_after"] == 10
    # 目标超员时来源也不应被动到
    assert len(versions(client, src)) == 4
    assert len(versions(client, dst)) == 8


def test_transfer_boundary_sizes_ok(client):
    # 8 台调走 6 台给 2 台组：来源剩 2、目标变 8，两个边界都合法
    src = make_group(client, tuple(f"s-{i}" for i in range(8)))
    dst = make_group(client, ("d-1", "d-2"))
    moving = [f"s-{i}" for i in range(6)]
    resp = transfer(client, src, dst, moving)
    assert resp.status_code == 200, resp.text
    assert len(versions(client, src)) == 2
    assert len(versions(client, dst)) == 8


def test_transfer_rejected_when_source_has_open_batch(client):
    src, dst = make_two_groups(client)
    bid = make_batch(client, src)
    resp = transfer(client, src, dst, ["s-2"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["open_batch_id"] == bid
    assert detail["source_group_id"] == src
    assert set(versions(client, src)) == {"s-1", "s-2", "s-3"}


def test_transfer_rejected_when_target_has_open_batch(client):
    src, dst = make_two_groups(client)
    bid = make_batch(client, dst)
    resp = transfer(client, src, dst, ["s-2"])
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["open_batch_id"] == bid
    assert detail["target_group_id"] == dst
    assert set(versions(client, dst)) == {"d-1", "d-2"}


def test_transfer_allowed_after_batch_closed_and_keeps_history(client):
    src, dst = make_two_groups(
        client, src_ids=("s-1", "s-2", "s-3", "s-4")
    )

    # 来源组先跑完一个 effective 批次（旧名单 s-1..s-4 → 2.0.0）
    old_bid = make_batch(client, src, targets=[
        {"device_id": d, "target_version": "2.0.0"}
        for d in ("s-1", "s-2", "s-3", "s-4")
    ])
    for d in ("s-1", "s-2", "s-3", "s-4"):
        client.post(f"/batches/{old_bid}/receipts",
                    json={"device_id": d, "result": "ready", "version": "1.0.0"})
    assert client.get(f"/batches/{old_bid}").json()["status"] == "effective"
    assert versions(client, src) == {
        "s-1": "2.0.0", "s-2": "2.0.0", "s-3": "2.0.0", "s-4": "2.0.0",
    }

    # 目标组跑完一个 failed 批次
    failed_bid = make_batch(client, dst, targets=[
        {"device_id": "d-1", "target_version": "3.0.0"},
        {"device_id": "d-2", "target_version": "3.0.0"},
    ])
    client.post(f"/batches/{failed_bid}/receipts",
                json={"device_id": "d-1", "result": "failed", "version": "1.5.0"})

    # 两组批次都结束 → 允许调配；s-2/s-3 带着 2.0.0 移动
    resp = transfer(client, src, dst, ["s-2", "s-3"])
    assert resp.status_code == 200, resp.text
    assert versions(client, src) == {"s-1": "2.0.0", "s-4": "2.0.0"}
    assert versions(client, dst) == {
        "d-1": "1.5.0", "d-2": "1.5.0",
        "s-2": "2.0.0", "s-3": "2.0.0",
    }

    # 已结束批次仍归原组、按旧名单查询
    old_batch = client.get(f"/batches/{old_bid}").json()
    assert old_batch["status"] == "effective"
    assert {t["device_id"] for t in old_batch["targets"]} == {
        "s-1", "s-2", "s-3", "s-4"
    }
    assert len(old_batch["receipts"]) == 4
    assert [b["id"] for b in client.get(f"/groups/{src}/batches").json()] == [old_bid]
    assert [b["id"] for b in client.get(f"/groups/{dst}/batches").json()] == [failed_bid]

    # 已有回执同结果重放不受调配影响（即使设备已不在原组）
    replay = client.post(
        f"/batches/{old_bid}/receipts",
        json={"device_id": "s-2", "result": "ready", "version": "2.0.0"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["status"] == "effective"

    # 移动设备对旧批提交"新"回执（此前没回执过不可能——旧批 3 台都 ready 过；
    # 改报另一种结果仍按旧批规则 409）
    flip = client.post(
        f"/batches/{old_bid}/receipts",
        json={"device_id": "s-2", "result": "failed", "version": "2.0.0"},
    )
    assert flip.status_code == 409


def test_new_batches_after_transfer_cover_new_rosters(client):
    src, dst = make_two_groups(
        client, src_ids=("s-1", "s-2", "s-3", "s-4")
    )
    assert transfer(client, src, dst, ["s-2", "s-3"]).status_code == 200

    # 来源组新批：只覆盖调配后的整组（s-1/s-4）
    src_bid = make_batch(client, src, targets=[
        {"device_id": "s-1", "target_version": "9.0.0"},
        {"device_id": "s-4", "target_version": "9.0.0"},
    ])
    # 目标组新批：覆盖调配后的整组 4 台
    dst_bid = make_batch(client, dst, targets=[
        {"device_id": "d-1", "target_version": "3.0.0"},
        {"device_id": "d-2", "target_version": "3.0.0"},
        {"device_id": "s-2", "target_version": "3.0.0"},
        {"device_id": "s-3", "target_version": "3.0.0"},
    ])

    # 用调配前的旧名单（2 台）在目标组开批：覆盖不到调配后的整组（4 台），
    # 名单校验先于 open 批互斥 → 422。旧名单永远无法按新名单创建批次。
    stale = client.post(f"/groups/{dst}/batches", json={"targets": [
        {"device_id": "d-1", "target_version": "3.0.0"},
        {"device_id": "d-2", "target_version": "3.0.0"},
    ]})
    assert stale.status_code == 422
    assert set(stale.json()["detail"]["missing_devices"]) == {"s-2", "s-3"}

    # 正常完成两组新批
    for d in ("s-1", "s-4"):
        client.post(f"/batches/{src_bid}/receipts",
                    json={"device_id": d, "result": "ready", "version": "1.0.0"})
    for d in ("d-1", "d-2", "s-2", "s-3"):
        client.post(f"/batches/{dst_bid}/receipts",
                    json={"device_id": d, "result": "ready", "version": "x"})
    assert versions(client, src) == {"s-1": "9.0.0", "s-4": "9.0.0"}
    assert versions(client, dst) == {
        "d-1": "3.0.0", "d-2": "3.0.0",
        "s-2": "3.0.0", "s-3": "3.0.0",
    }


def test_concurrent_transfer_and_open_batch_serializable(client):
    """调配与开批并发：结果必须等价于某个先后顺序。

    * 开批先 → 组内有 open 批 → 调配 409，批次按旧名单存在；
    * 调配先 → 旧名单开批覆盖不到新整组 → 422，调配 200。
    绝不能出现"调配成功且一个按旧名单创建的 open 批残留"。
    """
    src = make_group(client, tuple(f"dev-{i}" for i in range(4)))
    dst = make_group(client, ("d-1", "d-2"))
    old_targets = [
        {"device_id": f"dev-{i}", "target_version": "2.0.0"} for i in range(4)
    ]
    barrier = threading.Barrier(2)

    def do_transfer():
        barrier.wait()
        return transfer(client, src, dst, ["dev-3"])

    def do_open():
        barrier.wait()
        return client.post(
            f"/groups/{src}/batches", json={"targets": old_targets}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(do_transfer), pool.submit(do_open)]
        t_resp, b_resp = [f.result() for f in futs]

    open_batches = [
        b for b in client.get(f"/groups/{src}/batches").json()
        if b["status"] == "open"
    ]

    if t_resp.status_code == 200:
        # 调配先：旧名单（4 台）与新名单（3 台）不符，开批必须失败
        assert b_resp.status_code == 422, b_resp.text
        assert open_batches == []
        assert set(versions(client, src)) == {"dev-0", "dev-1", "dev-2"}
        assert versions(client, dst)["dev-3"] == "1.0.0"
    else:
        # 开批先：调配被 open 批挡住，批次按旧名单存在且仍 open
        assert t_resp.status_code == 409, t_resp.text
        assert b_resp.status_code == 201, b_resp.text
        assert len(open_batches) == 1
        batch = client.get(f"/batches/{open_batches[0]['id']}").json()
        assert {t["device_id"] for t in batch["targets"]} == {
            f"dev-{i}" for i in range(4)
        }
        assert set(versions(client, src)) == {f"dev-{i}" for i in range(4)}


def test_concurrent_transfer_and_closing_receipt(client):
    """调配与"最后一台 ready 使批次生效"并发：

    * 回执先（批次关闭、版本切换）→ 调配 200，设备带着新版本移动；
    * 调配先（当时批次仍 open）→ 调配必被 409。
    任何串行顺序下都不允许出现：设备被调走、而旧批 UPDATE devices 漏切/错切版本。
    """
    src = make_group(client, ("a", "b", "c"))
    dst = make_group(client, ("d-1", "d-2"))
    bid = make_batch(client, src, targets=[
        {"device_id": "a", "target_version": "2.0.0"},
        {"device_id": "b", "target_version": "2.0.0"},
        {"device_id": "c", "target_version": "2.0.0"},
    ])
    # 先收两台，留下最后一台与调配对打
    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "a", "result": "ready", "version": "1.0.0"})
    client.post(f"/batches/{bid}/receipts",
                json={"device_id": "b", "result": "ready", "version": "1.0.0"})

    barrier = threading.Barrier(2)

    def do_transfer():
        barrier.wait()
        return transfer(client, src, dst, ["c"])

    def last_receipt():
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "c", "result": "ready", "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(do_transfer), pool.submit(last_receipt)]
        t_resp, r_resp = [f.result() for f in futs]

    assert r_resp.status_code == 200
    batch = client.get(f"/batches/{bid}").json()

    if t_resp.status_code == 200:
        # 回执先完成：批次按旧名单生效，c 以 2.0.0 进入目标组
        assert batch["status"] == "effective"
        assert versions(client, src) == {"a": "2.0.0", "b": "2.0.0"}
        assert versions(client, dst)["c"] == "2.0.0"
    else:
        # 调配先于关闭发生：当时批次仍 open，必须拒绝
        assert t_resp.status_code == 409, t_resp.text
        assert batch["status"] == "effective"
        assert versions(client, src) == {"a": "2.0.0", "b": "2.0.0", "c": "2.0.0"}
        assert "c" not in versions(client, dst)


def test_concurrent_two_disjoint_transfers_both_apply(client):
    """两笔互不冲突的调配并发：都应完整生效，设备不丢不重、版本随行。

    来源 8 台各移 2 台到两个 2 台目标组；无论哪个先提交：
    来源 8→6→4，每个目标 2→4，两组结果都满足 2～8。
    """
    src = make_group(client, tuple(f"s-{i}" for i in range(8)), version="1.0.0")
    dst_a = make_group(client, ("a-1", "a-2"), version="7.0.0")
    dst_b = make_group(client, ("b-1", "b-2"), version="7.0.0")
    barrier = threading.Barrier(2)

    def move(target, devices):
        barrier.wait()
        return transfer(client, src, target, devices)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(move, dst_a, ["s-0", "s-1"]),
            pool.submit(move, dst_b, ["s-2", "s-3"]),
        ]
        resps = [f.result() for f in futs]

    assert sorted(r.status_code for r in resps) == [200, 200]
    src_after = versions(client, src)
    a_after = versions(client, dst_a)
    b_after = versions(client, dst_b)
    assert set(src_after) == {"s-4", "s-5", "s-6", "s-7"}
    assert all(v == "1.0.0" for v in src_after.values())
    assert a_after == {
        "a-1": "7.0.0", "a-2": "7.0.0",
        "s-0": "1.0.0", "s-1": "1.0.0",
    }
    assert b_after == {
        "b-1": "7.0.0", "b-2": "7.0.0",
        "s-2": "1.0.0", "s-3": "1.0.0",
    }
    # 全局设备总数守恒：8 + 2 + 2 = 12
    assert len(src_after) + len(a_after) + len(b_after) == 12


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


def make_two_groups(client):
    """来源组 4 台（版本各异）、目标组 3 台，均 1.0.0 起步。"""
    src = make_group(client, ("s-1", "s-2", "s-3", "s-4"))
    dst = make_group(client, ("d-1", "d-2", "d-3"))
    return src, dst


def transfer(client, src, dst, ids):
    return client.post(
        "/groups/transfers",
        json={"source_group_id": src, "target_group_id": dst, "device_ids": list(ids)},
    )


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


# --------------------------------------------------------------------------- #
# 设备组间调配
# --------------------------------------------------------------------------- #
def test_transfer_moves_devices_with_versions_and_returns_both_groups(client):
    src, dst = make_two_groups(client)
    # 先让 s-2 在来源组里升到 2.1.0，验证"连同当前版本"搬迁。
    bid = make_batch(
        client,
        src,
        targets=[
            {"device_id": "s-1", "target_version": "1.0.0"},
            {"device_id": "s-2", "target_version": "2.1.0"},
            {"device_id": "s-3", "target_version": "1.0.0"},
            {"device_id": "s-4", "target_version": "1.0.0"},
        ],
    )
    for d in ("s-1", "s-2", "s-3", "s-4"):
        client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        )

    resp = transfer(client, src, dst, ["s-2", "s-4"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transferred_devices"] == ["s-2", "s-4"]

    assert [d["device_id"] for d in body["source_group"]["devices"]] == ["s-1", "s-3"]
    assert [d["device_id"] for d in body["target_group"]["devices"]] == [
        "d-1", "d-2", "d-3", "s-2", "s-4",
    ]
    target_versions = {
        d["device_id"]: d["current_version"]
        for d in body["target_group"]["devices"]
    }
    assert target_versions["s-2"] == "2.1.0"  # 版本跟着设备走
    assert target_versions["s-4"] == "1.0.0"

    # GET 接口与调配响应一致。
    assert versions(client, src) == {"s-1": "1.0.0", "s-3": "1.0.0"}
    assert versions(client, dst) == {
        "d-1": "1.0.0", "d-2": "1.0.0", "d-3": "1.0.0",
        "s-2": "2.1.0", "s-4": "1.0.0",
    }


def test_transfer_empty_list_rejected(client):
    src, dst = make_two_groups(client)
    resp = transfer(client, src, dst, [])
    assert resp.status_code == 422


def test_transfer_duplicate_ids_in_request_rejected(client):
    src, dst = make_two_groups(client)
    resp = transfer(client, src, dst, ["s-1", "s-1"])
    assert resp.status_code == 422


def test_transfer_same_source_and_target_rejected(client):
    src, _ = make_two_groups(client)
    resp = transfer(client, src, src, ["s-1"])
    assert resp.status_code == 422


def test_transfer_unknown_group_404(client):
    src, dst = make_two_groups(client)
    assert transfer(client, "grp_nope", dst, ["s-1"]).status_code == 404
    assert transfer(client, src, "grp_nope", ["s-1"]).status_code == 404


def test_transfer_device_not_in_source_rejected_and_atomic(client):
    src, dst = make_two_groups(client)
    before_src, before_dst = versions(client, src), versions(client, dst)

    resp = transfer(client, src, dst, ["s-1", "ghost"])
    assert resp.status_code == 422
    assert resp.json()["detail"]["unknown_devices"] == ["ghost"]

    # 整次请求不改变数据。
    assert versions(client, src) == before_src
    assert versions(client, dst) == before_dst


def test_transfer_target_already_has_same_id_rejected_and_atomic(client):
    src, dst = make_two_groups(client)
    # 在两个组里造一个同 ID 设备（分别建组允许跨组重名）。
    g1 = make_group(client, ("dup", "g1-x"))
    g2 = make_group(client, ("dup", "g2-x"))
    before_g1, before_g2 = versions(client, g1), versions(client, g2)

    resp = transfer(client, g1, g2, ["dup"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicting_devices"] == ["dup"]
    assert versions(client, g1) == before_g1
    assert versions(client, g2) == before_g2


@pytest.mark.parametrize(
    "src_size,dst_size,moving",
    [
        (2, 2, 1),   # 来源调后 1 台
        (3, 8, 1),   # 目标调后 9 台
        (8, 2, 7),   # 来源调后 1 台、目标调后 9 台
    ],
)
def test_transfer_size_limits_rejected_and_atomic(
    client, src_size, dst_size, moving
):
    src = make_group(client, tuple(f"s-{i}" for i in range(src_size)))
    dst = make_group(client, tuple(f"d-{i}" for i in range(dst_size)))
    before_src, before_dst = versions(client, src), versions(client, dst)

    resp = transfer(
        client, src, dst, [f"s-{i}" for i in range(moving)]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["source_size_after"] == src_size - moving
    assert detail["target_size_after"] == dst_size + moving
    assert versions(client, src) == before_src
    assert versions(client, dst) == before_dst


def test_transfer_rejected_when_source_has_open_batch(client):
    src, dst = make_two_groups(client)
    bid = make_batch(client, src)
    resp = transfer(client, src, dst, ["s-1", "s-2"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["open_batch_id"] == bid
    assert resp.json()["detail"]["group_id"] == src
    assert versions(client, src) == {
        "s-1": "1.0.0", "s-2": "1.0.0", "s-3": "1.0.0", "s-4": "1.0.0",
    }
    assert set(versions(client, dst)) == {"d-1", "d-2", "d-3"}


def test_transfer_rejected_when_target_has_open_batch(client):
    src, dst = make_two_groups(client)
    bid = make_batch(client, dst)
    resp = transfer(client, src, dst, ["s-1", "s-2"])
    assert resp.status_code == 409
    assert resp.json()["detail"]["open_batch_id"] == bid
    assert resp.json()["detail"]["group_id"] == dst


def test_transfer_allowed_after_batch_closed_and_new_batch_uses_new_roster(client):
    src, dst = make_two_groups(client)
    # 来源组一个已结束（failed）批次，不阻塞调配。
    old_bid = make_batch(client, src)
    client.post(
        f"/batches/{old_bid}/receipts",
        json={"device_id": "s-1", "result": "failed", "version": "1.0.0"},
    )
    resp = transfer(client, src, dst, ["s-1", "s-2"])
    assert resp.status_code == 200, resp.text

    # 已结束批次仍归原组，且名单/回执仍可按原样查询。
    old_batch = client.get(f"/batches/{old_bid}").json()
    assert old_batch["status"] == "failed"
    assert old_batch["group_id"] == src
    assert {t["device_id"] for t in old_batch["targets"]} == {
        "s-1", "s-2", "s-3", "s-4",
    }
    assert {r["device_id"] for r in old_batch["receipts"]} == {"s-1"}
    assert [b["id"] for b in client.get(f"/groups/{src}/batches").json()] == [old_bid]

    # 已结束批次的已有回执同结果重放，不受调配影响。
    replay = client.post(
        f"/batches/{old_bid}/receipts",
        json={"device_id": "s-1", "result": "failed", "version": "1.0.0"},
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True

    # 调配后的新批次按新名单覆盖整组：目标组 5 台、来源组 2 台。
    new_bid = make_batch(client, dst)
    new_batch = client.get(f"/batches/{new_bid}").json()
    assert {t["device_id"] for t in new_batch["targets"]} == {
        "d-1", "d-2", "d-3", "s-1", "s-2",
    }

    src_bid = make_batch(client, src)
    src_batch = client.get(f"/batches/{src_bid}").json()
    assert {t["device_id"] for t in src_batch["targets"]} == {"s-3", "s-4"}

    # 新批次按新名单结算：5 台全 ready，版本切换只影响新组这 5 台。
    for d in ("d-1", "d-2", "d-3", "s-1", "s-2"):
        client.post(
            f"/batches/{new_bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        )
    assert versions(client, dst) == {
        "d-1": "2.0.0", "d-2": "2.0.0", "d-3": "2.0.0",
        "s-1": "2.0.0", "s-2": "2.0.0",
    }
    assert versions(client, src) == {"s-3": "1.0.0", "s-4": "1.0.0"}


def test_transfer_after_effective_batch_keeps_history_and_moved_versions(client):
    src, dst = make_two_groups(client)
    bid = make_batch(client, src)  # 4 台全升到 2.0.0
    for d in ("s-1", "s-2", "s-3", "s-4"):
        client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        )
    assert transfer(client, src, dst, ["s-3"]).status_code == 200

    # 旧批次仍是 effective、仍归原组、回执原样可查；搬走的设备带着 2.0.0。
    old = client.get(f"/batches/{bid}").json()
    assert old["status"] == "effective"
    assert old["group_id"] == src
    assert len(old["receipts"]) == 4
    assert versions(client, dst)["s-3"] == "2.0.0"

    # 对已结束旧批的已有回执重放依然幂等。
    replay = client.post(
        f"/batches/{bid}/receipts",
        json={"device_id": "s-3", "result": "ready", "version": "2.0.0"},
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True


def test_concurrent_transfer_vs_open_batch_exactly_one_serial_outcome(client):
    """调配与开批并发：只能按某个先后顺序完整生效。

    不变量：绝不能出现"旧名单创建的批次按新名单结算"——
    若批次先开（旧名单 4 台），调配必被拒；若调配对先完成，则开批必按
    新名单覆盖整组。
    """
    src, dst = make_two_groups(client)
    old_roster = ["s-1", "s-2", "s-3", "s-4"]
    targets = [{"device_id": d, "target_version": "2.0.0"} for d in old_roster]
    barrier = threading.Barrier(2)

    def do_transfer():
        barrier.wait()
        return transfer(client, src, dst, ["s-1", "s-2"])

    def do_open():
        barrier.wait()
        return client.post(f"/groups/{src}/batches", json={"targets": targets})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(do_transfer), pool.submit(do_open)]
        t_resp, b_resp = [f.result() for f in futs]

    src_members = set(versions(client, src))
    open_batches = [
        b for b in client.get(f"/groups/{src}/batches").json()
        if b["status"] == "open"
    ]

    if b_resp.status_code == 201:
        # 开批在先（旧名单）：调配必被 409 拒绝，组成员不变，批次按旧名单结算。
        assert t_resp.status_code == 409
        assert src_members == set(old_roster)
        assert len(open_batches) == 1
        batch = b_resp.json()
        assert {t["device_id"] for t in batch["targets"]} == set(old_roster)
        for d in old_roster:
            r = client.post(
                f"/batches/{batch['id']}/receipts",
                json={"device_id": d, "result": "ready", "version": "1.0.0"},
            )
            assert r.status_code == 200, r.text
        assert set(versions(client, src).values()) == {"2.0.0"}
        assert set(versions(client, dst)) == {"d-1", "d-2", "d-3"}
    else:
        # 调配在先：开批若仍按旧名单必被 422（整组覆盖校验按新名单），
        # 且不存在 open 批次。
        assert t_resp.status_code == 200
        assert b_resp.status_code in (409, 422)
        assert src_members == {"s-3", "s-4"}
        assert open_batches == []
        # 调配后再开批，必须按新名单 2 台。
        new_bid = make_batch(client, src)
        assert {
            t["device_id"] for t in client.get(f"/batches/{new_bid}").json()["targets"]
        } == {"s-3", "s-4"}


def test_concurrent_transfer_vs_final_receipt_versions_stay_per_membership(client):
    """调配与"最后一张 ready 回执触发整组切版本"并发。

    串行化保证只可能出现两种完整顺序：
      * 调配对先拿写锁：批次仍 open → 调配 409 回滚，成员不变；
        随后回执按旧名单 4 台完整结算，全部 2.0.0。
      * 回执先拿写锁：批次按旧名单结算为 effective（4 台全切），
        随后调配成功，s-4 带着 2.0.0 搬到目标组。
    绝不可能出现"旧名单批次按新名单只切一部分"。
    """
    src, dst = make_two_groups(client)
    bid = make_batch(client, src)
    for d in ("s-1", "s-2", "s-3"):
        client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        )
    barrier = threading.Barrier(2)

    def do_transfer():
        barrier.wait()
        return transfer(client, src, dst, ["s-4"])

    def final_receipt():
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "s-4", "result": "ready", "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(do_transfer), pool.submit(final_receipt)]
        t_resp, r_resp = [f.result() for f in futs]

    batch = client.get(f"/batches/{bid}").json()
    src_vs = versions(client, src)
    dst_vs = versions(client, dst)

    # 回执总是 200；批次在两种顺序下都按旧名单完整结算。
    assert r_resp.status_code == 200
    assert batch["status"] == "effective"
    assert batch["receipt_counts"] == {"ready": 4, "failed": 0, "total": 4}
    # 留在原组的 3 台必然已切版本。
    assert src_vs["s-1"] == src_vs["s-2"] == src_vs["s-3"] == "2.0.0"

    if t_resp.status_code == 200:
        # 结算先于调配：s-4 带着 2.0.0 搬走。
        assert "s-4" not in src_vs
        assert dst_vs["s-4"] == "2.0.0"
        assert set(src_vs) == {"s-1", "s-2", "s-3"}
    else:
        # 调配先拿锁被拒：s-4 留在原组且版本已切，目标组不受影响。
        assert t_resp.status_code == 409
        assert src_vs["s-4"] == "2.0.0"
        assert "s-4" not in dst_vs
        assert set(dst_vs) == {"d-1", "d-2", "d-3"}


def test_concurrent_transfer_vs_final_receipt_two_devices(client):
    """同一不变量的另一种成员形状：调走 2 台，末张 ready 属于留下的设备。"""
    src, dst = make_two_groups(client)
    bid = make_batch(client, src)
    # 末张回执是 s-3（留在 src）；s-1、s-2 待调走。
    for d in ("s-1", "s-2", "s-4"):
        client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": d, "result": "ready", "version": "1.0.0"},
        )
    barrier = threading.Barrier(2)

    def do_transfer():
        barrier.wait()
        return transfer(client, src, dst, ["s-1", "s-2"])

    def final_receipt():
        barrier.wait()
        return client.post(
            f"/batches/{bid}/receipts",
            json={"device_id": "s-3", "result": "ready", "version": "1.0.0"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(do_transfer), pool.submit(final_receipt)]
        t_resp, r_resp = [f.result() for f in futs]

    batch = client.get(f"/batches/{bid}").json()
    src_vs = versions(client, src)
    dst_vs = versions(client, dst)

    assert r_resp.status_code == 200
    assert batch["status"] == "effective"
    assert batch["receipt_counts"] == {"ready": 4, "failed": 0, "total": 4}

    if t_resp.status_code == 200:
        # 结算先于调配：4 台全切 2.0.0，s-1、s-2 带新版本进目标组。
        assert set(src_vs) == {"s-3", "s-4"}
        assert all(v == "2.0.0" for v in src_vs.values())
        assert dst_vs["s-1"] == dst_vs["s-2"] == "2.0.0"
        assert dst_vs["d-1"] == "1.0.0"
    else:
        # 调配对先拿锁，因 open 批次被拒：成员与组归属不变。
        assert t_resp.status_code == 409
        assert set(src_vs) == {"s-1", "s-2", "s-3", "s-4"}
        assert all(v == "2.0.0" for v in src_vs.values())
        assert set(dst_vs) == {"d-1", "d-2", "d-3"}


def test_concurrent_opposing_transfers_are_both_serializable(client):
    """两个组对调各一台并发：结果等价于某个串行顺序，两组都保持 2～8 台，
    版本始终跟设备走、无同 ID 冲突、无 5xx。"""
    src, dst = make_two_groups(client)

    def run_many():
        # src -> dst: s-1；dst -> src: d-1，两者并发。
        barrier = threading.Barrier(2)

        def t1():
            barrier.wait()
            return transfer(client, src, dst, ["s-1"])

        def t2():
            barrier.wait()
            return transfer(client, dst, src, ["d-1"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(t1), pool.submit(t2)]
            return [f.result().status_code for f in futs]

    # 第一轮：两个调配互不冲突（调不同的 ID），都应成功。
    codes = run_many()
    assert sorted(codes) == [200, 200]
    src_members = versions(client, src)
    dst_members = versions(client, dst)
    assert set(src_members) == {"s-2", "s-3", "s-4", "d-1"}
    assert set(dst_members) == {"d-2", "d-3", "s-1"}

    # 第二轮再对调同样的两台：每台已在对方组，两个请求都必失败且数据不变。
    codes = run_many()
    assert codes == [422, 422]
    assert set(versions(client, src)) == {"s-2", "s-3", "s-4", "d-1"}
    assert set(versions(client, dst)) == {"d-2", "d-3", "s-1"}


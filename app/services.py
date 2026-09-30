"""业务逻辑层：所有写操作都在单个 BEGIN IMMEDIATE 事务内完成。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from . import db

RECEIPT_RESULTS = ("ready", "failed")
TERMINAL_STATUS = ("effective", "failed")

# 设备组成员数约束（与 schemas.CreateGroupRequest 保持一致）。
MIN_GROUP_SIZE = 2
MAX_GROUP_SIZE = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# --------------------------------------------------------------------------- #
# 设备组
# --------------------------------------------------------------------------- #
def create_group(name: str, devices: list[dict[str, str]]) -> dict[str, Any]:
    group_id = _new_id("grp")
    created_at = _now()
    try:
        with db.write_transaction() as conn:
            conn.execute(
                "INSERT INTO device_groups(id, name, created_at) VALUES (?, ?, ?)",
                (group_id, name, created_at),
            )
            conn.executemany(
                "INSERT INTO devices(group_id, device_id, current_version, created_at)"
                " VALUES (?, ?, ?, ?)",
                [
                    (group_id, d["device_id"], d["current_version"], created_at)
                    for d in devices
                ],
            )
    except sqlite3.IntegrityError as exc:  # 理论上被模型层挡住，保留兜底
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    return get_group(group_id)


def list_groups() -> list[dict[str, Any]]:
    with db.read_transaction() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at FROM device_groups ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def _group_row(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, created_at FROM device_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"设备组不存在: {group_id}"})
    return row


def get_group(group_id: str) -> dict[str, Any]:
    with db.read_transaction() as conn:
        group = dict(_group_row(conn, group_id))
        devices = [
            dict(r)
            for r in conn.execute(
                "SELECT device_id, current_version FROM devices"
                " WHERE group_id = ? ORDER BY device_id",
                (group_id,),
            ).fetchall()
        ]
    group["devices"] = devices
    return group


def _group_devices(conn: sqlite3.Connection, group_id: str) -> list[dict[str, Any]]:
    """在已有事务内读取组内设备（调配后返回两组新名单时复用同一写事务快照）。"""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT device_id, current_version FROM devices"
            " WHERE group_id = ? ORDER BY device_id",
            (group_id,),
        ).fetchall()
    ]


def transfer_devices(
    source_group_id: str, target_group_id: str, device_ids: list[str]
) -> dict[str, Any]:
    """把来源组内的一批设备连当前版本整体调到目标组。

    全部校验与移动在同一个 ``BEGIN IMMEDIATE`` 事务内完成，任一条件不满足即
    抛异常回滚，数据一台都不动：
      * 两组必须存在且互不相同；
      * 每台设备都必须在来源组内（陌生设备 → 422）；
      * 目标组不能已有同 ID 设备（设备 ID 全局唯一 → 422）；
      * 调配后两组的成员数都必须落在 2～8 之间（→ 422）；
      * 任一组存在未结束（open）批次 → 409。

    并发语义：调配与开批、回执共用全局唯一写锁，事务内检查两组 open 批次，
    因此三方的任何交错都等价于某个串行顺序。若开批先于调配，则该组存在
    open 批次、调配被拒（旧名单的批次不会在新名单上结算）；若调配先于开批，
    之后的批次只能按新名单创建。已结束批次的 group_id 与目标版本原样保留，
    永远归原组、按旧名单查询与重放。
    """
    requested = sorted(set(device_ids))
    with db.write_transaction() as conn:
        _group_row(conn, source_group_id)  # 来源组不存在 → 404
        _group_row(conn, target_group_id)  # 目标组不存在 → 404
        if source_group_id == target_group_id:
            raise HTTPException(
                status_code=422,
                detail={"message": "来源组与目标组必须是两个不同的设备组"},
            )

        source_devices = _group_devices(conn, source_group_id)
        target_devices = _group_devices(conn, target_group_id)
        source_ids = {d["device_id"] for d in source_devices}
        target_ids = {d["device_id"] for d in target_devices}

        unknown = sorted(set(requested) - source_ids)
        conflicts = sorted(set(requested) & target_ids)

        problems: dict[str, Any] = {}
        if unknown:
            problems["unknown_devices"] = unknown
        if conflicts:
            problems["target_conflict_devices"] = conflicts
        if not problems:
            new_source_size = len(source_ids) - len(requested)
            new_target_size = len(target_ids) + len(requested)
            if not (MIN_GROUP_SIZE <= new_source_size <= MAX_GROUP_SIZE):
                problems["source_group"] = {
                    "group_id": source_group_id,
                    "size_after": new_source_size,
                    "min": MIN_GROUP_SIZE,
                    "max": MAX_GROUP_SIZE,
                }
            if not (MIN_GROUP_SIZE <= new_target_size <= MAX_GROUP_SIZE):
                problems["target_group"] = {
                    "group_id": target_group_id,
                    "size_after": new_target_size,
                    "min": MIN_GROUP_SIZE,
                    "max": MAX_GROUP_SIZE,
                }
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"message": "设备调配条件不满足，请求未改动任何数据", **problems},
            )

        # 任一组尚有未结束批次都拒绝。开批与本事务互斥（写锁 + open 部分唯一
        # 索引），因此这里的检查不会与开批交错出"旧名单批次按新名单结算"。
        for label, group_id in (
            ("source_group_id", source_group_id),
            ("target_group_id", target_group_id),
        ):
            open_batch = conn.execute(
                "SELECT id FROM batches WHERE group_id = ? AND status = 'open'",
                (group_id,),
            ).fetchone()
            if open_batch is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "任一组存在尚未结束的批次时不能调配设备",
                        label: group_id,
                        "open_batch_id": open_batch["id"],
                    },
                )

        # 设备连同 current_version 一起移动：只改 group_id，版本与登记时间不动。
        # 已结束批次的 batch_targets/receipts 不引用 devices 行，历史批次不受影响。
        cur = conn.executemany(
            "UPDATE devices SET group_id = ?"
            " WHERE group_id = ? AND device_id = ?",
            [
                (target_group_id, source_group_id, device_id)
                for device_id in requested
            ],
        )
        if cur.rowcount != len(requested):
            # 上面已逐台校验，理论上不可能；兜底防止任何静默少移动。
            raise HTTPException(status_code=500, detail={"message": "调配行数与请求不一致"})

        source_payload = dict(
            conn.execute(
                "SELECT id, name, created_at FROM device_groups WHERE id = ?",
                (source_group_id,),
            ).fetchone()
        )
        target_payload = dict(
            conn.execute(
                "SELECT id, name, created_at FROM device_groups WHERE id = ?",
                (target_group_id,),
            ).fetchone()
        )
        source_payload["devices"] = _group_devices(conn, source_group_id)
        target_payload["devices"] = _group_devices(conn, target_group_id)

    return {
        "source_group": source_payload,
        "target_group": target_payload,
        "transferred_devices": requested,
    }


# --------------------------------------------------------------------------- #
# 放行批次
# --------------------------------------------------------------------------- #
def create_batch(group_id: str, targets: list[dict[str, str]]) -> dict[str, Any]:
    batch_id = _new_id("bat")
    created_at = _now()
    try:
        with db.write_transaction() as conn:
            _group_row(conn, group_id)  # 组不存在 → 404

            members = {
                r["device_id"]: r["current_version"]
                for r in conn.execute(
                    "SELECT device_id, current_version FROM devices WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
            }
            target_ids = {t["device_id"] for t in targets}
            unknown = sorted(target_ids - members.keys())
            missing = sorted(members.keys() - target_ids)
            if unknown or missing:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "目标设备必须逐台覆盖整组",
                        "unknown_devices": unknown,
                        "missing_devices": missing,
                    },
                )

            open_batch = conn.execute(
                "SELECT id FROM batches WHERE group_id = ? AND status = 'open'",
                (group_id,),
            ).fetchone()
            if open_batch is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "该组存在尚未结束的批次，不能开新批",
                        "open_batch_id": open_batch["id"],
                    },
                )

            conn.execute(
                "INSERT INTO batches(id, group_id, status, created_at)"
                " VALUES (?, ?, 'open', ?)",
                (batch_id, group_id, created_at),
            )
            conn.executemany(
                "INSERT INTO batch_targets(batch_id, device_id, target_version)"
                " VALUES (?, ?, ?)",
                [
                    (batch_id, t["device_id"], t["target_version"])
                    for t in targets
                ],
            )
    except sqlite3.IntegrityError as exc:
        # 并发开新批时，ux_open_batch_per_group 兜底：第二个提交者冲突。
        raise HTTPException(
            status_code=409,
            detail={"message": "该组存在尚未结束的批次，不能开新批"},
        ) from exc
    return get_batch(batch_id)


def list_batches(group_id: str) -> list[dict[str, Any]]:
    with db.read_transaction() as conn:
        _group_row(conn, group_id)
        rows = conn.execute(
            "SELECT id, group_id, status, created_at, closed_at FROM batches"
            " WHERE group_id = ? ORDER BY created_at",
            (group_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _batch_row(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, group_id, status, created_at, closed_at FROM batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"批次不存在: {batch_id}"})
    return row


def _batch_payload(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    batch = dict(_batch_row(conn, batch_id))
    targets = [
        dict(r)
        for r in conn.execute(
            "SELECT device_id, target_version FROM batch_targets"
            " WHERE batch_id = ? ORDER BY device_id",
            (batch_id,),
        ).fetchall()
    ]
    receipts = [
        dict(r)
        for r in conn.execute(
            "SELECT device_id, result, version, created_at FROM receipts"
            " WHERE batch_id = ? ORDER BY device_id",
            (batch_id,),
        ).fetchall()
    ]
    batch["targets"] = targets
    batch["receipts"] = receipts
    batch["receipt_counts"] = {
        "ready": sum(1 for r in receipts if r["result"] == "ready"),
        "failed": sum(1 for r in receipts if r["result"] == "failed"),
        "total": len(targets),
    }
    return batch


def get_batch(batch_id: str) -> dict[str, Any]:
    with db.read_transaction() as conn:
        return _batch_payload(conn, batch_id)


# --------------------------------------------------------------------------- #
# 设备回执
# --------------------------------------------------------------------------- #
def submit_receipt(
    batch_id: str, device_id: str, result: str, version: str
) -> dict[str, Any]:
    """提交一台设备的回执。

    幂等与冲突规则：
      * 同一设备、同一结果重放         → 200（replayed=true），任何时候都接受；
      * 同一设备、改报另一种结果       → 409，批次状态不变；
      * 批次结束后的"迟到新回执"       → 409，旧批结果与后续批次均不受影响；
      * 设备不在批次名单内             → 422。
    批次推进规则（全部在同一写事务内）：
      * 任一 failed        → 批次 failed，所有当前版本保持原样；
      * 全部 ready         → 原子切换每台设备的当前版本，批次标记 effective。
    """
    assert result in RECEIPT_RESULTS
    created_at = _now()
    with db.write_transaction() as conn:
        batch = _batch_row(conn, batch_id)
        group_id, status = batch["group_id"], batch["status"]

        target = conn.execute(
            "SELECT target_version FROM batch_targets"
            " WHERE batch_id = ? AND device_id = ?",
            (batch_id, device_id),
        ).fetchone()
        if target is None:
            raise HTTPException(
                status_code=422,
                detail={"message": f"设备 {device_id} 不在批次 {batch_id} 的名单内"},
            )

        existing = conn.execute(
            "SELECT result FROM receipts WHERE batch_id = ? AND device_id = ?",
            (batch_id, device_id),
        ).fetchone()
        if existing is not None:
            # 重放：同结果幂等成功；改报另一结果冲突。
            if existing["result"] != result:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "同一设备不能改报另一种结果",
                        "existing_result": existing["result"],
                        "submitted_result": result,
                    },
                )
            payload = _batch_payload(conn, batch_id)
            payload["replayed"] = True
            return payload

        if status in TERMINAL_STATUS:
            # 批次已结束，只接受已有回执的重放；迟到的新回执拒绝且无副作用。
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "批次已结束，旧批次的迟到回执不再接受",
                    "batch_status": status,
                },
            )

        try:
            conn.execute(
                "INSERT INTO receipts(batch_id, device_id, result, version, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, device_id, result, version, created_at),
            )
        except sqlite3.IntegrityError:
            # 同一设备的两份回执并发到达：两者都在插入前读到"无回执"，
            # 败者在此撞主键。等价于紧接胜者之后重放——重读已落库回执，
            # 按"同结果幂等 / 改报冲突"处理，而不是 500。
            winner = conn.execute(
                "SELECT result FROM receipts WHERE batch_id = ? AND device_id = ?",
                (batch_id, device_id),
            ).fetchone()
            if winner is not None and winner["result"] != result:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "同一设备不能改报另一种结果",
                        "existing_result": winner["result"],
                        "submitted_result": result,
                    },
                )
            payload = _batch_payload(conn, batch_id)
            payload["replayed"] = True
            return payload

        if result == "failed":
            # 任一 failed：批次立即失败。不触碰 devices 表，版本保持原样。
            conn.execute(
                "UPDATE batches SET status = 'failed', closed_at = ? WHERE id = ?",
                (created_at, batch_id),
            )
        else:
            missing = conn.execute(
                "SELECT 1 FROM batch_targets bt"
                " WHERE bt.batch_id = ?"
                "   AND NOT EXISTS (SELECT 1 FROM receipts r"
                "                     WHERE r.batch_id = bt.batch_id"
                "                       AND r.device_id = bt.device_id"
                "                       AND r.result = 'ready')"
                " LIMIT 1",
                (batch_id,),
            ).fetchone()
            if missing is None:
                # 全部 ready：同事务原子切换所有版本，再标记生效。
                # 没有任何中间状态可以被其他事务看到。
                conn.execute(
                    "UPDATE devices SET current_version = ("
                    "    SELECT bt.target_version FROM batch_targets bt"
                    "    WHERE bt.batch_id = ? AND bt.device_id = devices.device_id"
                    ") WHERE group_id = ?",
                    (batch_id, group_id),
                )
                conn.execute(
                    "UPDATE batches SET status = 'effective', closed_at = ?"
                    " WHERE id = ?",
                    (created_at, batch_id),
                )

        payload = _batch_payload(conn, batch_id)
        payload["replayed"] = False
        return payload

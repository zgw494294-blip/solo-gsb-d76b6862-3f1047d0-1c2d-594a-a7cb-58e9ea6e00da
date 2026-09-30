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

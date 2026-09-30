"""本地固件成组放行 HTTP API。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, status

from . import db, services
from .schemas import CreateBatchRequest, CreateGroupRequest, ReceiptRequest


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    db.init_db()
    yield


app = FastAPI(
    title="固件成组放行 API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------- 设备组 ------------------------- #
@app.post("/groups", status_code=status.HTTP_201_CREATED)
def create_group(request: CreateGroupRequest) -> dict:
    return services.create_group(
        name=request.name,
        devices=[d.model_dump() for d in request.devices],
    )


@app.get("/groups")
def list_groups() -> list[dict]:
    return services.list_groups()


@app.get("/groups/{group_id}")
def get_group(group_id: str) -> dict:
    """查询设备组及其当前版本（每台设备一行）。"""
    return services.get_group(group_id)


# ------------------------- 批次 ------------------------- #
@app.post("/groups/{group_id}/batches", status_code=status.HTTP_201_CREATED)
def create_batch(group_id: str, request: CreateBatchRequest) -> dict:
    return services.create_batch(
        group_id=group_id,
        targets=[t.model_dump() for t in request.targets],
    )


@app.get("/groups/{group_id}/batches")
def list_batches(group_id: str) -> list[dict]:
    return services.list_batches(group_id)


@app.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict:
    """查询批次状态、逐台目标版本与逐台回执。"""
    return services.get_batch(batch_id)


# ------------------------- 回执 ------------------------- #
@app.post("/batches/{batch_id}/receipts")
def submit_receipt(batch_id: str, request: ReceiptRequest) -> dict:
    # 首次提交与幂等重放均返回 200，由响应体 replayed 字段区分。
    return services.submit_receipt(
        batch_id=batch_id,
        device_id=request.device_id,
        result=request.result,
        version=request.version,
    )

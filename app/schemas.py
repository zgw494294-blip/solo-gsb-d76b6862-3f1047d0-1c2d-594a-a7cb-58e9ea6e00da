"""Pydantic 请求模型。"""

from __future__ import annotations

from typing import Annotated

from typing_extensions import Literal

from pydantic import BaseModel, Field, field_validator

DeviceId = Annotated[str, Field(min_length=1, max_length=128)]
Version = Annotated[str, Field(min_length=1, max_length=128)]
GroupName = Annotated[str, Field(min_length=1, max_length=200)]


class RegisterDevice(BaseModel):
    device_id: DeviceId
    current_version: Version


class CreateGroupRequest(BaseModel):
    name: GroupName
    devices: list[RegisterDevice] = Field(min_length=2, max_length=8)

    @field_validator("devices")
    @classmethod
    def _unique_device_ids(cls, devices: list[RegisterDevice]) -> list[RegisterDevice]:
        ids = [d.device_id for d in devices]
        if len(set(ids)) != len(ids):
            raise ValueError("设备 ID 在同一组内必须唯一，不能重复登记")
        return devices


class BatchTarget(BaseModel):
    device_id: DeviceId
    target_version: Version


class CreateBatchRequest(BaseModel):
    # 必须逐台给出目标版本；缺一台或多一台都会被拒绝。
    targets: list[BatchTarget] = Field(min_length=2, max_length=8)

    @field_validator("targets")
    @classmethod
    def _unique_device_ids(cls, targets: list[BatchTarget]) -> list[BatchTarget]:
        ids = [t.device_id for t in targets]
        if len(set(ids)) != len(targets):
            raise ValueError("批次目标中的设备 ID 不能重复")
        return targets


class ReceiptRequest(BaseModel):
    device_id: DeviceId
    result: Literal["ready", "failed"]
    # 设备回执时自报的当前版本，原样记录在案，便于审计。
    version: Version

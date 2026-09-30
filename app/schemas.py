"""Pydantic 请求模型。"""

from __future__ import annotations

from typing import Annotated

from typing_extensions import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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


GroupId = Annotated[str, Field(min_length=1, max_length=128)]


class TransferDevicesRequest(BaseModel):
    """把一批设备从 source_group_id 调到 target_group_id。"""

    source_group_id: GroupId
    target_group_id: GroupId
    # 来源组内一批互不重复的设备 ID；列表不能为空，且一次最多整组 8 台。
    device_ids: list[DeviceId] = Field(min_length=1, max_length=8)

    @field_validator("device_ids")
    @classmethod
    def _unique_device_ids(cls, device_ids: list[str]) -> list[str]:
        if len(set(device_ids)) != len(device_ids):
            raise ValueError("待调配的设备 ID 不能重复")
        return device_ids

    @model_validator(mode="after")
    def _two_different_groups(self) -> "TransferDevicesRequest":
        if self.source_group_id == self.target_group_id:
            raise ValueError("来源组与目标组必须是两个不同的设备组")
        return self


class ReceiptRequest(BaseModel):
    device_id: DeviceId
    result: Literal["ready", "failed"]
    # 设备回执时自报的当前版本，原样记录在案，便于审计。
    version: Version

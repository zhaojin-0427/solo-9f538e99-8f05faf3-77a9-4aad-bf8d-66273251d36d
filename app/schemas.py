"""HTTP 入参模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


class CorrectionIn(BaseModel):
    turbine_id: str = Field(..., min_length=1)
    utc_date: str = Field(..., pattern=DATE_RE, description="被更正分区的 UTC 日期")

    @field_validator("turbine_id")
    @classmethod
    def _turbine_nonblank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("turbine_id 不能为空")
        return v.strip()


class PartitionIn(CorrectionIn):
    revision: int = Field(..., ge=1, description="递增分区修订号，同一风机日期必须严格递增")


class WindowIn(BaseModel):
    turbine_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    end_date: str = Field(..., pattern=DATE_RE, description="窗口结束日，窗口取该日及前两日")
    version: int | None = Field(None, ge=1, description="不传则取当前最大版本号 + 1")


class SliceWindowRefIn(BaseModel):
    turbine_id: str = Field(..., min_length=1)
    feature_name: str = Field(..., min_length=1)
    end_date: str = Field(..., pattern=DATE_RE)
    version: int = Field(..., ge=1, description="引用的窗口版本号（按所见版本登记）")


class SliceIn(BaseModel):
    turbine_id: str = Field(..., min_length=1)
    slice_name: str = Field(..., min_length=1)
    version: int | None = Field(None, ge=1, description="不传则取当前最大版本号 + 1")
    window_refs: list[SliceWindowRefIn] = Field(..., min_length=1)


class PreviewIn(BaseModel):
    corrections: list[CorrectionIn] = Field(..., min_length=1)

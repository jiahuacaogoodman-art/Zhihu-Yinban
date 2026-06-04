# -*- coding: utf-8 -*-
"""
@File    : app/models/care_schemas.py
@Desc    : 护理业务模块 Pydantic Schema —— 床位管理、护理等级、交接班、异常事件、护理记录

模块清单（必做第一阶段）：
  1. 床位管理 (Bed)           — 床位分配、状态管理
  2. 护理等级 (Care Level)    — 等级定义及与老人的关联
  3. 交接班 (Handover/SBAR)   — 独立 SBAR 格式交接记录
  4. 异常事件上报 (Incident)  — 事件类型、严重等级、处理流程
  5. 护理记录留痕 (Care Record) — 操作记录、质控追溯
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# 通用混入
# ============================================================
class _CodeMessage(BaseModel):
    code: int = 200
    message: Optional[str] = None


# ============================================================
# 1. 床位管理 (Bed Management)
# ============================================================
BedStatus = Literal["available", "occupied", "maintenance", "reserved"]


class BedCreate(BaseModel):
    """POST /api/beds"""
    bed_number: str = Field(..., min_length=1, max_length=32, description="床位编号，如 A-101")
    floor: Optional[str] = Field(default=None, max_length=32, description="楼层，如 1F")
    building: Optional[str] = Field(default=None, max_length=64, description="楼栋，如 A栋")
    room: Optional[str] = Field(default=None, max_length=32, description="房间号，如 101")
    bed_type: Optional[str] = Field(default="standard", max_length=32, description="床位类型: standard/electric/icu")
    notes: Optional[str] = Field(default=None, max_length=256)


class BedUpdate(BaseModel):
    """PATCH /api/beds/{bed_id}"""
    bed_number: Optional[str] = Field(default=None, max_length=32)
    floor: Optional[str] = Field(default=None, max_length=32)
    building: Optional[str] = Field(default=None, max_length=64)
    room: Optional[str] = Field(default=None, max_length=32)
    bed_type: Optional[str] = Field(default=None, max_length=32)
    status: Optional[BedStatus] = None
    notes: Optional[str] = Field(default=None, max_length=256)


class BedAssign(BaseModel):
    """POST /api/beds/{bed_id}/assign"""
    patient_id: str = Field(..., min_length=1, max_length=64)


class BedResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bed_id: str
    bed_number: str
    floor: Optional[str] = None
    building: Optional[str] = None
    room: Optional[str] = None
    bed_type: str = "standard"
    status: BedStatus = "available"
    patient_id: Optional[str] = None
    patient_name: Optional[str] = None
    assigned_at: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class BedListResponse(_CodeMessage):
    total: int
    beds: List[BedResponse] = Field(default_factory=list)


# ============================================================
# 2. 护理等级 (Care Level)
# ============================================================
class CareLevelCreate(BaseModel):
    """POST /api/care-levels"""
    level_key: str = Field(..., min_length=1, max_length=32, description="等级标识, 如 level_1")
    level_name: str = Field(..., min_length=1, max_length=64, description="等级名称, 如 一级护理")
    description: Optional[str] = Field(default="", max_length=512)
    daily_fee: Optional[float] = Field(default=None, ge=0, description="日收费标准(元)")
    service_items: Optional[str] = Field(default=None, description="服务项目(逗号分隔)")
    min_nurse_ratio: Optional[str] = Field(default=None, max_length=32, description="最低护工配比, 如 1:5")
    sort_order: Optional[int] = Field(default=0, description="排序权重, 越小越优先")


class CareLevelUpdate(BaseModel):
    """PATCH /api/care-levels/{level_key}"""
    level_name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=512)
    daily_fee: Optional[float] = Field(default=None, ge=0)
    service_items: Optional[str] = None
    min_nurse_ratio: Optional[str] = Field(default=None, max_length=32)
    sort_order: Optional[int] = None


class CareLevelResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    level_id: str
    level_key: str
    level_name: str
    description: str = ""
    daily_fee: Optional[float] = None
    service_items: Optional[str] = None
    min_nurse_ratio: Optional[str] = None
    sort_order: int = 0
    resident_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class CareLevelListResponse(_CodeMessage):
    total: int
    levels: List[CareLevelResponse] = Field(default_factory=list)


class CareLevelAssign(BaseModel):
    """POST /api/care-levels/assign"""
    patient_id: str = Field(..., min_length=1, max_length=64)
    level_key: str = Field(..., min_length=1, max_length=32)
    reason: Optional[str] = Field(default=None, max_length=256, description="调整原因")
    assessed_by: Optional[str] = Field(default=None, max_length=64)


# ============================================================
# 3. 交接班 (Handover / SBAR)
# ============================================================
class HandoverCreate(BaseModel):
    """POST /api/handovers"""
    shift_from: str = Field(..., min_length=1, max_length=64, description="交班人")
    shift_to: str = Field(..., min_length=1, max_length=64, description="接班人")
    shift_type: Optional[str] = Field(default="day_to_night", max_length=32,
                                       description="班次类型: day_to_night/night_to_day/special")
    patient_id: Optional[str] = Field(default=None, max_length=64, description="关联患者(可空=全区交接)")
    situation: str = Field(..., min_length=1, description="S - 现状: 发生了什么")
    background: str = Field(..., min_length=1, description="B - 背景: 既往病史/用药等")
    assessment: str = Field(..., min_length=1, description="A - 评估: 当前风险判断")
    recommendation: str = Field(..., min_length=1, description="R - 建议: 接班后需做什么")
    pending_tasks: Optional[str] = Field(default=None, description="未完成事项(逗号分隔)")
    notes: Optional[str] = Field(default=None, max_length=512)


class HandoverResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handover_id: str
    shift_from: str
    shift_to: str
    shift_type: str = "day_to_night"
    patient_id: Optional[str] = None
    patient_name: Optional[str] = None
    situation: str
    background: str
    assessment: str
    recommendation: str
    pending_tasks: Optional[str] = None
    notes: Optional[str] = None
    status: str = "pending"  # pending / acknowledged / completed
    acknowledged_at: Optional[str] = None
    created_at: str = ""


class HandoverAcknowledge(BaseModel):
    """PATCH /api/handovers/{handover_id}/acknowledge"""
    acknowledged_by: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=256)


class HandoverListResponse(_CodeMessage):
    total: int
    handovers: List[HandoverResponse] = Field(default_factory=list)


# ============================================================
# 4. 异常事件上报 (Incident Report)
# ============================================================
IncidentSeverity = Literal["critical", "major", "minor", "observation"]
IncidentStatus = Literal["reported", "processing", "resolved", "closed"]


class IncidentCreate(BaseModel):
    """POST /api/incidents"""
    patient_id: str = Field(..., min_length=1, max_length=64)
    incident_type: str = Field(..., min_length=1, max_length=64,
                                description="事件类型: 跌倒/误吸/走失/烫伤/压疮/用药错误/其他")
    severity: IncidentSeverity = Field(default="minor")
    description: str = Field(..., min_length=1, description="事件详细描述")
    location: Optional[str] = Field(default=None, max_length=128, description="发生地点")
    occurred_at: Optional[str] = Field(default=None, description="事件发生时间(可晚于上报)")
    reporter: Optional[str] = Field(default=None, max_length=64, description="上报人")
    witnesses: Optional[str] = Field(default=None, max_length=256, description="目击者(逗号分隔)")
    immediate_action: Optional[str] = Field(default=None, description="已采取的紧急措施")


class IncidentUpdate(BaseModel):
    """PATCH /api/incidents/{incident_id}"""
    severity: Optional[IncidentSeverity] = None
    status: Optional[IncidentStatus] = None
    description: Optional[str] = None
    follow_up: Optional[str] = Field(default=None, description="后续处理措施")
    root_cause: Optional[str] = Field(default=None, description="根因分析")
    prevention: Optional[str] = Field(default=None, description="预防改进措施")
    resolved_by: Optional[str] = Field(default=None, max_length=64)
    resolved_at: Optional[str] = None
    notes: Optional[str] = None


class IncidentResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    incident_id: str
    patient_id: str
    patient_name: Optional[str] = None
    incident_type: str
    severity: IncidentSeverity
    status: IncidentStatus = "reported"
    description: str
    location: Optional[str] = None
    occurred_at: Optional[str] = None
    reporter: Optional[str] = None
    witnesses: Optional[str] = None
    immediate_action: Optional[str] = None
    follow_up: Optional[str] = None
    root_cause: Optional[str] = None
    prevention: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


class IncidentListResponse(_CodeMessage):
    total: int
    incidents: List[IncidentResponse] = Field(default_factory=list)


class IncidentStats(BaseModel):
    """GET /api/incidents/stats"""
    total: int = 0
    by_severity: dict = Field(default_factory=dict)
    by_type: dict = Field(default_factory=dict)
    by_status: dict = Field(default_factory=dict)
    period: str = ""


# ============================================================
# 5. 护理记录留痕 (Care Record / Nursing Log)
# ============================================================
CareRecordType = Literal[
    "vital_signs",       # 生命体征
    "daily_care",        # 日常护理
    "medication",        # 用药记录
    "diet",              # 饮食记录
    "activity",          # 活动记录
    "observation",       # 观察记录
    "special_care",      # 特殊护理
    "other",             # 其他
]


class CareRecordCreate(BaseModel):
    """POST /api/care-records"""
    patient_id: str = Field(..., min_length=1, max_length=64)
    record_type: CareRecordType = Field(default="observation")
    content: str = Field(..., min_length=1, description="记录内容")
    vital_data: Optional[str] = Field(default=None, description="生命体征JSON: {bp,hr,temp,spo2,bg}")
    recorded_by: Optional[str] = Field(default=None, max_length=64)
    recorded_at: Optional[str] = Field(default=None, description="实际记录时间(可补录)")
    shift: Optional[str] = Field(default=None, max_length=32, description="班次: day/night/swing")
    related_event_id: Optional[str] = Field(default=None, max_length=64, description="关联事件ID")
    notes: Optional[str] = Field(default=None, max_length=512)


class CareRecordResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    record_id: str
    patient_id: str
    patient_name: Optional[str] = None
    record_type: str
    content: str
    vital_data: Optional[str] = None
    recorded_by: Optional[str] = None
    recorded_at: str = ""
    shift: Optional[str] = None
    related_event_id: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = ""


class CareRecordListResponse(_CodeMessage):
    total: int
    records: List[CareRecordResponse] = Field(default_factory=list)


__all__ = [
    # Bed
    "BedCreate", "BedUpdate", "BedAssign", "BedResponse", "BedListResponse",
    # Care Level
    "CareLevelCreate", "CareLevelUpdate", "CareLevelResponse",
    "CareLevelListResponse", "CareLevelAssign",
    # Handover
    "HandoverCreate", "HandoverResponse", "HandoverAcknowledge", "HandoverListResponse",
    # Incident
    "IncidentCreate", "IncidentUpdate", "IncidentResponse",
    "IncidentListResponse", "IncidentStats",
    # Care Record
    "CareRecordCreate", "CareRecordResponse", "CareRecordListResponse",
]

# ============================================================
# 6. 运营总览（养老院管理驾驶舱）
# ============================================================
class OperationsOverviewResponse(BaseModel):
    """GET /api/operations/overview"""
    total_beds: int = 0
    occupied_beds: int = 0
    occupancy_rate: float = 0.0
    total_residents: int = 0
    pending_admissions: int = 0
    open_incidents: int = 0
    pending_handovers: int = 0
    today_care_records: int = 0

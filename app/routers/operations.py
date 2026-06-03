# -*- coding: utf-8 -*-
"""
@File    : app/routers/operations.py
@Desc    : 养老院运营总览接口（管理驾驶舱基础能力）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware.auth import require_permission
from app.models.care_schemas import OperationsOverviewResponse
from app.services.care_store import get_care_store
from app.services.permissions import PERM_EHR_READ
from app.services.user_store import User

router = APIRouter()


@router.get("/operations/overview", response_model=OperationsOverviewResponse, summary="运营总览")
async def operations_overview(
    user: User = Depends(require_permission(PERM_EHR_READ)),
):
    """返回养老院日常运营管理最常用的总览指标。"""
    store = get_care_store()
    return OperationsOverviewResponse(**store.get_operations_overview())

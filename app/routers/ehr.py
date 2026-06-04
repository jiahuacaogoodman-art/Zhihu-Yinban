# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status
from loguru import logger

from app.core.config import EHR_UPLOAD_DIR, MAX_UPLOAD_SIZE_MB, ALLOWED_UPLOAD_EXTENSIONS
from app.middleware.auth import get_current_user, require_permission
from app.models.schemas import (
    EHRAddRequest, EHRAddResponse,
    EHRUpdateRequest, EHRUpdateResponse,
    EHRDeleteRequest, EHRDeleteResponse,
    EHRRecord,
)
from app.services.audit_log import get_audit_log, _diff_meta
from app.services.ocr_service import LocalOCRService
from app.services.permissions import PERM_EHR_AUDIT_READ
from app.services.pii_crypto import PII_FIELDS, decrypt_pii_fields, encrypt_pii_fields
from app.services.user_store import User

router = APIRouter()
ocr_service = LocalOCRService()
audit = get_audit_log()

UPLOAD_ROOT = Path(EHR_UPLOAD_DIR)
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)

PROFILE_DOC_TYPE = "patient_profile"
UPLOAD_DOC_TYPE = "medical_record_upload"

META_FIELDS = [
    "patient_id", "name", "age", "gender", "birth_date", "id_card",
    "admission_date", "emergency_contact", "emergency_phone", "emergency_relation",
    "height_cm", "weight_kg", "blood_type", "care_level", "bed_number",
    "primary_nurse", "medical_history", "allergy", "diet_restriction", "notes", "doc_type",
    "record_type", "original_filename", "stored_filename", "file_path", "file_url",
    "ocr_text_path", "ocr_status", "ocr_engine", "ocr_error", "uploaded_at",
    "content_type", "file_size", "manual_text",
]


def _get_state():
    from main import app_state
    collection = app_state.get("db_collection")
    embedding_function = app_state.get("embedding_function")
    if collection is None or embedding_function is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="数据库服务未就绪，请稍后重试")
    return collection, embedding_function


def _safe_filename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {".", "_", "-"} else "_" for ch in name).strip("._")
    return cleaned or "medical_record"


def _patient_upload_dir(patient_id: str) -> Path:
    root = UPLOAD_ROOT / _safe_filename(patient_id)
    (root / "photos").mkdir(parents=True, exist_ok=True)
    (root / "ocr").mkdir(parents=True, exist_ok=True)
    return root


def _build_metadata(payload: dict) -> dict:
    meta = {}
    for field in META_FIELDS:
        val = payload.get(field)
        if val is not None:
            meta[field] = val if isinstance(val, (str, int, float, bool)) else str(val)
    return encrypt_pii_fields(meta)


def _build_document(payload: dict) -> str:
    parts = [f"患者姓名：{payload.get('name', '')}，编号：{payload.get('patient_id', '')}"]
    mapping = [
        ("age", lambda v: f"年龄：{v}岁"),
        ("gender", lambda v: f"性别：{v}"),
        ("birth_date", lambda v: f"出生日期：{v}"),
        ("blood_type", lambda v: f"血型：{v}"),
        ("care_level", lambda v: f"护理等级：{v}"),
        ("bed_number", lambda v: f"床位号：{v}"),
        ("primary_nurse", lambda v: f"主管护工：{v}"),
        ("allergy", lambda v: f"过敏史：{v}"),
        ("diet_restriction", lambda v: f"饮食禁忌：{v}"),
        ("medical_history", lambda v: f"既往病史及用药：{v}"),
        ("notes", lambda v: f"备注：{v}"),
    ]
    for key, render in mapping:
        if payload.get(key):
            parts.append(render(payload[key]))
    if payload.get("height_cm") or payload.get("weight_kg"):
        parts.append(f"身高：{payload.get('height_cm', '—')}cm，体重：{payload.get('weight_kg', '—')}kg")
    if payload.get("emergency_contact"):
        parts.append(
            f"紧急联系人：{payload.get('emergency_contact', '')}"
            f"（{payload.get('emergency_relation', '')}），电话：{payload.get('emergency_phone', '')}"
        )
    return "；".join(parts)


def _build_upload_document(meta: dict, ocr_text: str, manual_text: Optional[str] = None) -> str:
    parts = [
        f"【病历照片OCR档案】患者姓名：{meta.get('name', '')}，编号：{meta.get('patient_id', '')}",
        f"病历类型：{meta.get('record_type', '未分类')}",
        f"原始文件：{meta.get('original_filename', '')}",
        f"上传时间：{meta.get('uploaded_at', '')}",
    ]
    if meta.get("notes"):
        parts.append(f"备注：{meta['notes']}")
    parts.append("OCR识别文本：\n" + (ocr_text.strip() or "未识别到有效文字或本地OCR引擎未配置"))
    if manual_text and manual_text.strip():
        parts.append("人工补充/校正文书：\n" + manual_text.strip())
    return "\n".join(parts)


def _is_profile(meta: dict) -> bool:
    return meta.get("doc_type") in (None, "", PROFILE_DOC_TYPE)


def _is_upload(meta: dict) -> bool:
    return meta.get("doc_type") == UPLOAD_DOC_TYPE


def _meta_to_record(doc_id: str, document: str, meta: dict) -> EHRRecord:
    meta = decrypt_pii_fields(meta or {})
    return EHRRecord(
        doc_id=doc_id,
        patient_id=meta.get("patient_id", ""),
        name=meta.get("name", ""),
        age=int(meta["age"]) if meta.get("age") is not None else None,
        gender=meta.get("gender"),
        birth_date=meta.get("birth_date"),
        id_card=meta.get("id_card"),
        admission_date=meta.get("admission_date"),
        emergency_contact=meta.get("emergency_contact"),
        emergency_phone=meta.get("emergency_phone"),
        emergency_relation=meta.get("emergency_relation"),
        height_cm=float(meta["height_cm"]) if meta.get("height_cm") is not None else None,
        weight_kg=float(meta["weight_kg"]) if meta.get("weight_kg") is not None else None,
        blood_type=meta.get("blood_type"),
        care_level=meta.get("care_level"),
        bed_number=meta.get("bed_number"),
        primary_nurse=meta.get("primary_nurse"),
        medical_history=meta.get("medical_history") or document,
        allergy=meta.get("allergy"),
        diet_restriction=meta.get("diet_restriction"),
        notes=meta.get("notes"),
    )


def _find_patient_name(collection, patient_id: str) -> Optional[str]:
    result = collection.get(where={"patient_id": {"$eq": patient_id}}, include=["metadatas"])
    for meta in result.get("metadatas", []):
        if _is_profile(meta or {}) and meta.get("name"):
            return meta.get("name")
    for meta in result.get("metadatas", []):
        if meta.get("name"):
            return meta.get("name")
    return None


def _add_document(collection, embedding_function, doc_id: str, document: str, metadata: dict) -> None:
    collection.add(ids=[doc_id], documents=[document], embeddings=[embedding_function.encode(document).tolist()], metadatas=[metadata])


async def _create_patient(payload: EHRAddRequest, user: User) -> EHRAddResponse:
    collection, embedding_function = _get_state()
    doc_id = f"{payload.patient_id}_{uuid.uuid4().hex[:8]}"
    data = payload.model_dump()
    data["doc_type"] = PROFILE_DOC_TYPE
    document = _build_document(data)
    try:
        _add_document(collection, embedding_function, doc_id, document, _build_metadata(data))
        audit.log("PATIENT_CREATE", payload.patient_id, user.username, doc_id=doc_id, detail=f"新建患者档案: {payload.name}")
        return EHRAddResponse(code=200, message=f"患者 {payload.name} 的档案已成功录入", patient_id=payload.patient_id, doc_id=doc_id)
    except Exception as e:
        logger.error(f"档案录入失败: {e}")
        raise HTTPException(status_code=500, detail=f"录入失败: {e}")


@router.post("/ehr/patients", summary="新增患者基本档案")
async def create_patient(payload: EHRAddRequest, user: User = Depends(get_current_user)):
    return await _create_patient(payload, user)


@router.get("/ehr/patients", summary="查询患者基本档案列表")
async def list_patients(user: User = Depends(get_current_user)):
    collection, _ = _get_state()
    result = collection.get(include=["documents", "metadatas"])
    records, seen = [], set()
    for doc_id, doc, meta in zip(result.get("ids", []), result.get("documents", []), result.get("metadatas", [])):
        if not _is_profile(meta or {}):
            continue
        pid = (meta or {}).get("patient_id", "")
        if pid in seen:
            continue
        seen.add(pid)
        records.append(_meta_to_record(doc_id, doc, meta).model_dump())
    audit.log("PATIENT_LIST", "", user.username, detail=f"查询患者列表，共返回 {len(records)} 条")
    return records


@router.get("/ehr/patients/{patient_id}", summary="查询单个患者基本档案")
async def get_patient(patient_id: str, user: User = Depends(get_current_user)):
    collection, _ = _get_state()
    result = collection.get(where={"patient_id": {"$eq": patient_id}}, include=["documents", "metadatas"])
    for doc_id, doc, meta in zip(result.get("ids", []), result.get("documents", []), result.get("metadatas", [])):
        if _is_profile(meta or {}):
            record = _meta_to_record(doc_id, doc, meta).model_dump()
            audit.log("PATIENT_READ", patient_id, user.username, doc_id=doc_id, detail=f"查看患者基本档案: {record.get('name', '')} (来源=ehr)")
            return record
    raise HTTPException(status_code=404, detail=f"未找到 patient_id='{patient_id}' 的患者基本档案")


@router.put("/ehr/patients/{patient_id}", summary="修改患者基本档案")
async def update_patient(patient_id: str, payload: dict = Body(...), user: User = Depends(get_current_user)):
    payload["patient_id"] = patient_id
    return await _update_patient(EHRUpdateRequest(**payload), user)


async def _update_patient(payload: EHRUpdateRequest, user: User) -> EHRUpdateResponse:
    collection, embedding_function = _get_state()
    result = collection.get(where={"patient_id": {"$eq": payload.patient_id}}, include=["documents", "metadatas"])
    existing_ids, old_meta, old_doc = [], {}, ""
    for doc_id, doc, meta in zip(result.get("ids", []), result.get("documents", []), result.get("metadatas", [])):
        if _is_profile(meta or {}):
            existing_ids.append(doc_id)
            if not old_meta:
                old_meta, old_doc = meta or {}, doc or ""
    if not existing_ids:
        raise HTTPException(status_code=404, detail=f"未找到 patient_id='{payload.patient_id}' 的基本档案")

    old_plain = decrypt_pii_fields(old_meta)
    new_data = payload.model_dump(exclude_unset=True)
    new_data.pop("patient_id", None)
    masked = [
        f for f in PII_FIELDS
        if f not in new_data
        and isinstance(old_plain.get(f), str)
        and old_plain[f].startswith(("[加密数据-需配置", "[解密失败"))
    ]
    if masked:
        raise HTTPException(status_code=503, detail="PII 字段无法解密（请检查 PII_ENCRYPTION_KEY 配置），修改已中止")

    merged = dict(old_plain)
    merged.setdefault("medical_history", old_doc)
    for k, v in new_data.items():
        if v is not None:
            merged[k] = v
    merged["patient_id"] = payload.patient_id
    merged["doc_type"] = PROFILE_DOC_TYPE
    new_doc_id = f"{payload.patient_id}_{uuid.uuid4().hex[:8]}"
    collection.delete(ids=existing_ids)
    _add_document(collection, embedding_function, new_doc_id, _build_document(merged), _build_metadata(merged))
    audit.log("PATIENT_UPDATE", payload.patient_id, user.username, doc_id=new_doc_id, detail=f"修改患者档案: {merged.get('name', '')}", diff=_diff_meta(old_plain, merged, list(merged.keys())))
    return EHRUpdateResponse(code=200, message=f"患者 {merged.get('name', payload.patient_id)} 的基本档案已更新", patient_id=payload.patient_id, updated_count=len(existing_ids))


@router.delete("/ehr/patients/{patient_id}", summary="删除患者全部档案")
async def delete_patient(patient_id: str, user: User = Depends(get_current_user)):
    return await _delete_patient(EHRDeleteRequest(patient_id=patient_id), user)


async def _delete_patient(payload: EHRDeleteRequest, user: User) -> EHRDeleteResponse:
    collection, _ = _get_state()
    result = collection.get(where={"patient_id": {"$eq": payload.patient_id}}, include=["metadatas"])
    existing_ids = result.get("ids", [])
    if not existing_ids:
        raise HTTPException(status_code=404, detail=f"未找到 patient_id='{payload.patient_id}' 的档案")
    patient_dir = UPLOAD_ROOT / _safe_filename(payload.patient_id)
    if patient_dir.exists():
        shutil.rmtree(patient_dir, ignore_errors=True)
    collection.delete(ids=existing_ids)
    audit.log("PATIENT_DELETE", payload.patient_id, user.username, detail=f"删除患者全部档案，共 {len(existing_ids)} 条记录")
    return EHRDeleteResponse(code=200, message=f"患者 {payload.patient_id} 的全部档案、病历照片与 OCR 文本已删除", patient_id=payload.patient_id, deleted_count=len(existing_ids))


@router.post("/ehr/records/upload", summary="上传病历照片并进行本地 OCR 识别")
async def upload_medical_records(
    patient_id: str = Form(...),
    name: Optional[str] = Form(None),
    record_type: str = Form("病历档案"),
    notes: Optional[str] = Form(None),
    manual_text: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    user: User = Depends(get_current_user),
):
    collection, embedding_function = _get_state()
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一张病历照片")
    patient_name = name or _find_patient_name(collection, patient_id) or ""
    root = _patient_upload_dir(patient_id)
    saved, max_bytes = [], MAX_UPLOAD_SIZE_MB * 1024 * 1024

    for file in files:
        original_name = file.filename or "medical_record.jpg"
        suffix = Path(original_name).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix}，请上传图片文件")
        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(status_code=413, detail=f"单个文件不能超过 {MAX_UPLOAD_SIZE_MB}MB")
        doc_id = f"{patient_id}_record_{uuid.uuid4().hex[:10]}"
        stored_name = f"{doc_id}{suffix}"
        photo_path = root / "photos" / stored_name
        photo_path.write_bytes(content)
        ocr = ocr_service.extract_text(photo_path)
        uploaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ocr_txt_path = root / "ocr" / f"{doc_id}.txt"
        ocr_txt_path.write_text(ocr.text or "", encoding="utf-8")
        rel_file_url = f"/uploads/{_safe_filename(patient_id)}/photos/{stored_name}"
        meta = {
            "patient_id": patient_id, "name": patient_name, "doc_type": UPLOAD_DOC_TYPE,
            "record_type": record_type, "notes": notes, "manual_text": manual_text,
            "original_filename": original_name, "stored_filename": stored_name,
            "file_path": str(photo_path), "file_url": rel_file_url,
            "ocr_text_path": str(ocr_txt_path), "ocr_status": ocr.status,
            "ocr_engine": ocr.engine, "ocr_error": ocr.error,
            "uploaded_at": uploaded_at, "content_type": file.content_type or "image/*",
            "file_size": len(content),
        }
        try:
            _add_document(collection, embedding_function, doc_id, _build_upload_document(meta, ocr.text, manual_text), _build_metadata(meta))
        except Exception:
            photo_path.unlink(missing_ok=True)
            ocr_txt_path.unlink(missing_ok=True)
            raise
        saved.append({"doc_id": doc_id, "patient_id": patient_id, "name": patient_name, "record_type": record_type, "original_filename": original_name, "file_url": rel_file_url, "ocr_text": ocr.text, "ocr_status": ocr.status, "ocr_engine": ocr.engine, "ocr_error": ocr.error, "uploaded_at": uploaded_at})

    for rec in saved:
        audit.log("RECORD_UPLOAD", patient_id, user.username, doc_id=rec["doc_id"], detail=f"上传病历照片: {rec['original_filename']} (ocr={rec['ocr_status']})")
    return {"code": 200, "message": f"已上传 {len(saved)} 份病历照片，并同步保存原图与 OCR 文本", "records": saved}


@router.get("/ehr/records/{patient_id}", summary="查询某患者的病历照片与 OCR 文本")
async def list_medical_records(patient_id: str, user: User = Depends(get_current_user)):
    collection, _ = _get_state()
    result = collection.get(where={"patient_id": {"$eq": patient_id}}, include=["documents", "metadatas"])
    records = []
    for doc_id, doc, meta in zip(result.get("ids", []), result.get("documents", []), result.get("metadatas", [])):
        if not _is_upload(meta or {}):
            continue
        p = meta.get("ocr_text_path")
        ocr_text = Path(p).read_text(encoding="utf-8", errors="ignore") if p and Path(p).exists() else (doc or "")
        records.append({"doc_id": doc_id, "patient_id": meta.get("patient_id"), "name": meta.get("name"), "record_type": meta.get("record_type"), "notes": meta.get("notes"), "original_filename": meta.get("original_filename"), "file_url": meta.get("file_url"), "ocr_status": meta.get("ocr_status"), "ocr_engine": meta.get("ocr_engine"), "ocr_error": meta.get("ocr_error"), "ocr_text": ocr_text, "manual_text": meta.get("manual_text"), "uploaded_at": meta.get("uploaded_at"), "file_size": meta.get("file_size")})
    records.sort(key=lambda x: x.get("uploaded_at") or "", reverse=True)
    audit.log("RECORD_READ", patient_id, user.username, detail=f"查询病历照片列表，共 {len(records)} 份")
    return {"code": 200, "patient_id": patient_id, "total": len(records), "records": records}


@router.delete("/ehr/records/{doc_id}", summary="删除单份病历照片档案")
async def delete_medical_record(doc_id: str, user: User = Depends(get_current_user)):
    collection, _ = _get_state()
    result = collection.get(ids=[doc_id], include=["metadatas"])
    if not result.get("ids", []):
        raise HTTPException(status_code=404, detail="未找到该病历照片档案")
    meta = result.get("metadatas", [{}])[0] or {}
    if not _is_upload(meta):
        raise HTTPException(status_code=400, detail="该 doc_id 不是病历照片档案，不能通过此接口删除")
    for key in ["file_path", "ocr_text_path"]:
        p = meta.get(key)
        if p:
            Path(p).unlink(missing_ok=True)
    collection.delete(ids=[doc_id])
    audit.log("RECORD_DELETE", meta.get("patient_id", ""), user.username, doc_id=doc_id, detail=f"删除病历照片: {meta.get('original_filename', '')}")
    return {"code": 200, "message": "病历照片档案已删除", "doc_id": doc_id}


@router.get("/ehr/audit", summary="查询操作审计日志（需要 ehr.audit_read 权限）")
async def get_audit_log(patient_id: Optional[str] = None, action: Optional[str] = None, limit: int = 100, _admin: User = Depends(require_permission(PERM_EHR_AUDIT_READ))):
    limit = min(limit, 500)
    records = audit.query(patient_id=patient_id, action=action, limit=limit)
    return {"code": 200, "total": len(records), "records": records}

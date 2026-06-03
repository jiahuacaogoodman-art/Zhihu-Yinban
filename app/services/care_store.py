# -*- coding: utf-8 -*-
"""
@File    : app/services/care_store.py
@Desc    : 护理业务数据持久化层 (SQLite WAL)

覆盖模块：
  - 床位管理 (beds)
  - 护理等级 (care_levels + care_level_assignments)
  - 交接班 (handovers)
  - 异常事件上报 (incidents)
  - 护理记录留痕 (care_records)
  - 入住流程 (admissions + assessments + contracts + payments + admission_timeline)

设计决策：
  - 单独 SQLite 数据库文件 local_care/care.db，与 users.db / audit.db 分离
  - WAL 模式支持多线程并发读写
  - 统一使用 threading.Lock 保证进程内线程安全
  - 所有时间字段用 ISO 格式字符串（与项目其他模块保持一致）
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger


class CareStore:
    """护理业务数据统一存储层。"""

    _CREATE_SQL = """
        -- 床位管理
        CREATE TABLE IF NOT EXISTS beds (
            bed_id      TEXT PRIMARY KEY,
            bed_number  TEXT NOT NULL UNIQUE,
            floor       TEXT NOT NULL DEFAULT '',
            building    TEXT NOT NULL DEFAULT '',
            room        TEXT NOT NULL DEFAULT '',
            bed_type    TEXT NOT NULL DEFAULT 'standard',
            status      TEXT NOT NULL DEFAULT 'available',
            patient_id  TEXT NOT NULL DEFAULT '',
            patient_name TEXT NOT NULL DEFAULT '',
            assigned_at TEXT NOT NULL DEFAULT '',
            notes       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_beds_number ON beds(bed_number);
        CREATE INDEX IF NOT EXISTS idx_beds_status ON beds(status);
        CREATE INDEX IF NOT EXISTS idx_beds_patient ON beds(patient_id);

        -- 护理等级
        CREATE TABLE IF NOT EXISTS care_levels (
            level_id        TEXT PRIMARY KEY,
            level_key       TEXT NOT NULL UNIQUE,
            level_name      TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            daily_fee       REAL,
            service_items   TEXT NOT NULL DEFAULT '',
            min_nurse_ratio TEXT NOT NULL DEFAULT '',
            sort_order      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_care_levels_key ON care_levels(level_key);

        -- 护理等级分配历史
        CREATE TABLE IF NOT EXISTS care_level_assignments (
            assignment_id TEXT PRIMARY KEY,
            patient_id    TEXT NOT NULL,
            level_key     TEXT NOT NULL,
            reason        TEXT NOT NULL DEFAULT '',
            assessed_by   TEXT NOT NULL DEFAULT '',
            assigned_at   TEXT NOT NULL,
            FOREIGN KEY (level_key) REFERENCES care_levels(level_key)
        );
        CREATE INDEX IF NOT EXISTS idx_cla_patient ON care_level_assignments(patient_id);

        -- 交接班
        CREATE TABLE IF NOT EXISTS handovers (
            handover_id     TEXT PRIMARY KEY,
            shift_from      TEXT NOT NULL,
            shift_to        TEXT NOT NULL,
            shift_type      TEXT NOT NULL DEFAULT 'day_to_night',
            patient_id      TEXT NOT NULL DEFAULT '',
            patient_name    TEXT NOT NULL DEFAULT '',
            situation       TEXT NOT NULL,
            background      TEXT NOT NULL,
            assessment      TEXT NOT NULL,
            recommendation  TEXT NOT NULL,
            pending_tasks   TEXT NOT NULL DEFAULT '',
            notes           TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            acknowledged_at TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_handovers_patient ON handovers(patient_id);
        CREATE INDEX IF NOT EXISTS idx_handovers_status ON handovers(status);
        CREATE INDEX IF NOT EXISTS idx_handovers_time ON handovers(created_at);

        -- 异常事件
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id      TEXT PRIMARY KEY,
            patient_id       TEXT NOT NULL,
            patient_name     TEXT NOT NULL DEFAULT '',
            incident_type    TEXT NOT NULL,
            severity         TEXT NOT NULL DEFAULT 'minor',
            status           TEXT NOT NULL DEFAULT 'reported',
            description      TEXT NOT NULL,
            location         TEXT NOT NULL DEFAULT '',
            occurred_at      TEXT NOT NULL DEFAULT '',
            reporter         TEXT NOT NULL DEFAULT '',
            witnesses        TEXT NOT NULL DEFAULT '',
            immediate_action TEXT NOT NULL DEFAULT '',
            follow_up        TEXT NOT NULL DEFAULT '',
            root_cause       TEXT NOT NULL DEFAULT '',
            prevention       TEXT NOT NULL DEFAULT '',
            resolved_by      TEXT NOT NULL DEFAULT '',
            resolved_at      TEXT NOT NULL DEFAULT '',
            notes            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_patient ON incidents(patient_id);
        CREATE INDEX IF NOT EXISTS idx_incidents_severity ON incidents(severity);
        CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
        CREATE INDEX IF NOT EXISTS idx_incidents_time ON incidents(created_at);

        -- 护理记录
        CREATE TABLE IF NOT EXISTS care_records (
            record_id        TEXT PRIMARY KEY,
            patient_id       TEXT NOT NULL,
            patient_name     TEXT NOT NULL DEFAULT '',
            record_type      TEXT NOT NULL DEFAULT 'observation',
            content          TEXT NOT NULL,
            vital_data       TEXT NOT NULL DEFAULT '',
            recorded_by      TEXT NOT NULL DEFAULT '',
            recorded_at      TEXT NOT NULL,
            shift            TEXT NOT NULL DEFAULT '',
            related_event_id TEXT NOT NULL DEFAULT '',
            notes            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_care_records_patient ON care_records(patient_id);
        CREATE INDEX IF NOT EXISTS idx_care_records_type ON care_records(record_type);
        CREATE INDEX IF NOT EXISTS idx_care_records_time ON care_records(recorded_at);

        -- ======== 入住流程 ========

        -- 入住申请主表
        CREATE TABLE IF NOT EXISTS admissions (
            admission_id            TEXT PRIMARY KEY,
            status                  TEXT NOT NULL DEFAULT 'inquiry',

            -- 申请人信息
            applicant_name          TEXT NOT NULL,
            applicant_gender        TEXT NOT NULL DEFAULT '',
            applicant_age           INTEGER,
            applicant_id_card       TEXT NOT NULL DEFAULT '',
            applicant_phone         TEXT NOT NULL DEFAULT '',

            -- 家属/担保人
            guardian_name           TEXT NOT NULL DEFAULT '',
            guardian_phone          TEXT NOT NULL DEFAULT '',
            guardian_relation       TEXT NOT NULL DEFAULT '',
            guardian_id_card        TEXT NOT NULL DEFAULT '',

            -- 健康/需求
            health_summary          TEXT NOT NULL DEFAULT '',
            care_needs              TEXT NOT NULL DEFAULT '',
            preferred_room_type     TEXT NOT NULL DEFAULT '',
            expected_admission_date TEXT NOT NULL DEFAULT '',

            -- 来源
            referral_source         TEXT NOT NULL DEFAULT '',
            notes                   TEXT NOT NULL DEFAULT '',

            -- 评估结果(冗余存储便于列表查询)
            assessment_id           TEXT NOT NULL DEFAULT '',
            assessed_level          TEXT NOT NULL DEFAULT '',
            assessment_conclusion   TEXT NOT NULL DEFAULT '',
            assessed_at             TEXT NOT NULL DEFAULT '',
            assessed_by             TEXT NOT NULL DEFAULT '',

            -- 合同信息(冗余)
            contract_id             TEXT NOT NULL DEFAULT '',
            contract_signed_at      TEXT NOT NULL DEFAULT '',

            -- 缴费信息(冗余)
            payment_id              TEXT NOT NULL DEFAULT '',
            payment_status          TEXT NOT NULL DEFAULT '',
            paid_at                 TEXT NOT NULL DEFAULT '',

            -- 入住信息
            patient_id              TEXT NOT NULL DEFAULT '',
            bed_id                  TEXT NOT NULL DEFAULT '',
            bed_number              TEXT NOT NULL DEFAULT '',
            care_level_key          TEXT NOT NULL DEFAULT '',
            actual_admission_date   TEXT NOT NULL DEFAULT '',

            -- 离院信息（持久化财务数据）
            discharge_date          TEXT NOT NULL DEFAULT '',
            discharge_reason        TEXT NOT NULL DEFAULT '',
            settlement_amount       REAL,
            refund_amount           REAL,

            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_admissions_status ON admissions(status);
        CREATE INDEX IF NOT EXISTS idx_admissions_name ON admissions(applicant_name);
        CREATE INDEX IF NOT EXISTS idx_admissions_patient ON admissions(patient_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_admissions_id_card_uniq
            ON admissions(applicant_id_card) WHERE applicant_id_card != '' AND status NOT IN ('discharged', 'cancelled');

        -- 评估记录
        CREATE TABLE IF NOT EXISTS assessments (
            assessment_id       TEXT PRIMARY KEY,
            admission_id        TEXT NOT NULL,
            adl_score           INTEGER,
            cognitive_score     INTEGER,
            nutrition_score     INTEGER,
            fall_risk_score     INTEGER,
            pressure_ulcer_risk INTEGER,
            recommended_level   TEXT NOT NULL,
            conclusion          TEXT NOT NULL,
            special_needs       TEXT NOT NULL DEFAULT '',
            assessor            TEXT NOT NULL DEFAULT '',
            assessment_date     TEXT NOT NULL,
            approved            INTEGER NOT NULL DEFAULT 1,
            created_at          TEXT NOT NULL,
            FOREIGN KEY (admission_id) REFERENCES admissions(admission_id)
        );
        CREATE INDEX IF NOT EXISTS idx_assessments_admission ON assessments(admission_id);

        -- 合同
        CREATE TABLE IF NOT EXISTS contracts (
            contract_id             TEXT PRIMARY KEY,
            admission_id            TEXT NOT NULL,
            contract_type           TEXT NOT NULL DEFAULT 'standard',
            contract_number         TEXT NOT NULL DEFAULT '',
            start_date              TEXT NOT NULL,
            end_date                TEXT NOT NULL DEFAULT '',
            care_level_key          TEXT NOT NULL,
            monthly_fee             REAL NOT NULL DEFAULT 0,
            deposit                 REAL NOT NULL DEFAULT 0,
            payment_cycle           TEXT NOT NULL DEFAULT 'monthly',
            service_scope           TEXT NOT NULL DEFAULT '',
            special_terms           TEXT NOT NULL DEFAULT '',
            signed_by_guardian      TEXT NOT NULL DEFAULT '',
            signed_by_institution   TEXT NOT NULL DEFAULT '',
            signed_at               TEXT NOT NULL DEFAULT '',
            status                  TEXT NOT NULL DEFAULT 'active',
            created_at              TEXT NOT NULL,
            FOREIGN KEY (admission_id) REFERENCES admissions(admission_id)
        );
        CREATE INDEX IF NOT EXISTS idx_contracts_admission ON contracts(admission_id);
        CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);

        -- 缴费记录
        CREATE TABLE IF NOT EXISTS payments (
            payment_id      TEXT PRIMARY KEY,
            admission_id    TEXT NOT NULL,
            contract_id     TEXT NOT NULL DEFAULT '',
            payment_type    TEXT NOT NULL DEFAULT 'deposit',
            amount          REAL NOT NULL,
            payment_method  TEXT NOT NULL DEFAULT 'cash',
            receipt_number  TEXT NOT NULL DEFAULT '',
            period_start    TEXT NOT NULL DEFAULT '',
            period_end      TEXT NOT NULL DEFAULT '',
            payer           TEXT NOT NULL DEFAULT '',
            notes           TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'completed',
            paid_at         TEXT NOT NULL,
            created_at      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_admission ON payments(admission_id);
        CREATE INDEX IF NOT EXISTS idx_payments_contract ON payments(contract_id);

        -- 入住流程时间线
        CREATE TABLE IF NOT EXISTS admission_timeline (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            admission_id    TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            action          TEXT NOT NULL,
            operator        TEXT NOT NULL DEFAULT '',
            detail          TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (admission_id) REFERENCES admissions(admission_id)
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_admission ON admission_timeline(admission_id);
    """

    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self._CREATE_SQL)
        logger.debug(f"CareStore 初始化完成: {self._path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _gen_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    # ================================================================
    # 床位管理
    # ================================================================
    def create_bed(self, data: dict) -> dict:
        bed_id = self._gen_id("bed")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO beds (bed_id, bed_number, floor, building, room, bed_type, "
                    "status, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (bed_id, data["bed_number"], data.get("floor") or "",
                     data.get("building") or "", data.get("room") or "",
                     data.get("bed_type") or "standard", "available",
                     data.get("notes") or "", now, now),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise ValueError(f"床位编号 '{data['bed_number']}' 已存在")
        return self.get_bed(bed_id)

    def get_bed(self, bed_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM beds WHERE bed_id = ?", (bed_id,)).fetchone()
        return dict(row) if row else None

    def get_bed_by_number(self, bed_number: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM beds WHERE bed_number = ?", (bed_number,)).fetchone()
        return dict(row) if row else None

    def list_beds(self, status: Optional[str] = None, building: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM beds WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if building:
            sql += " AND building = ?"
            params.append(building)
        sql += " ORDER BY building, floor, room, bed_number"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_bed(self, bed_id: str, data: dict) -> Optional[dict]:
        fields = []
        params = []
        for key in ("bed_number", "floor", "building", "room", "bed_type", "status", "notes"):
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return self.get_bed(bed_id)
        fields.append("updated_at = ?")
        params.append(self._now())
        params.append(bed_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(f"UPDATE beds SET {', '.join(fields)} WHERE bed_id = ?", params)
                if cur.rowcount == 0:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise ValueError(f"床位编号 '{data.get('bed_number')}' 已存在")
        return self.get_bed(bed_id)

    def assign_bed(self, bed_id: str, patient_id: str, patient_name: str = "") -> Optional[dict]:
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 先释放该患者之前占用的床位
            conn.execute(
                "UPDATE beds SET status='available', patient_id='', patient_name='', assigned_at='', updated_at=? "
                "WHERE patient_id = ?",
                (now, patient_id),
            )
            cur = conn.execute(
                "UPDATE beds SET status='occupied', patient_id=?, patient_name=?, assigned_at=?, updated_at=? "
                "WHERE bed_id = ? AND status IN ('available', 'reserved')",
                (patient_id, patient_name, now, now, bed_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return self.get_bed(bed_id)

    def release_bed(self, bed_id: str) -> Optional[dict]:
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE beds SET status='available', patient_id='', patient_name='', assigned_at='', updated_at=? "
                "WHERE bed_id = ?",
                (now, bed_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return self.get_bed(bed_id)

    def delete_bed(self, bed_id: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM beds WHERE bed_id = ? AND status != 'occupied'", (bed_id,))
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    # ================================================================
    # 护理等级
    # ================================================================
    def create_care_level(self, data: dict) -> dict:
        level_id = self._gen_id("lvl")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO care_levels (level_id, level_key, level_name, description, "
                    "daily_fee, service_items, min_nurse_ratio, sort_order, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (level_id, data["level_key"], data["level_name"],
                     data.get("description") or "", data.get("daily_fee"),
                     data.get("service_items") or "", data.get("min_nurse_ratio") or "",
                     data.get("sort_order") or 0, now, now),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                raise ValueError(f"护理等级 '{data['level_key']}' 已存在")
        return self.get_care_level(level_id)

    def get_care_level(self, level_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM care_levels WHERE level_id = ?", (level_id,)).fetchone()
        return dict(row) if row else None

    def get_care_level_by_key(self, level_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM care_levels WHERE level_key = ?", (level_key,)).fetchone()
        return dict(row) if row else None

    def list_care_levels(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM care_levels ORDER BY sort_order, level_key").fetchall()
            result = []
            for r in rows:
                d = dict(r)
                count = conn.execute(
                    "SELECT COUNT(*) as c FROM care_level_assignments WHERE level_key = ? "
                    "AND assignment_id IN (SELECT assignment_id FROM care_level_assignments "
                    "GROUP BY patient_id HAVING assigned_at = MAX(assigned_at))",
                    (d["level_key"],)
                ).fetchone()
                d["resident_count"] = count["c"] if count else 0
                result.append(d)
        return result

    def update_care_level(self, level_key: str, data: dict) -> Optional[dict]:
        fields = []
        params = []
        for key in ("level_name", "description", "daily_fee", "service_items", "min_nurse_ratio", "sort_order"):
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return self.get_care_level_by_key(level_key)
        fields.append("updated_at = ?")
        params.append(self._now())
        params.append(level_key)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(f"UPDATE care_levels SET {', '.join(fields)} WHERE level_key = ?", params)
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return self.get_care_level_by_key(level_key)

    def delete_care_level(self, level_key: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("DELETE FROM care_levels WHERE level_key = ?", (level_key,))
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return False
            conn.execute("COMMIT")
        return True

    def assign_care_level(self, patient_id: str, level_key: str, reason: str = "", assessed_by: str = "") -> dict:
        # Verify level exists
        level = self.get_care_level_by_key(level_key)
        if not level:
            raise ValueError(f"护理等级 '{level_key}' 不存在")
        assignment_id = self._gen_id("cla")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO care_level_assignments (assignment_id, patient_id, level_key, reason, assessed_by, assigned_at) "
                "VALUES (?,?,?,?,?,?)",
                (assignment_id, patient_id, level_key, reason, assessed_by, now),
            )
            conn.execute("COMMIT")
        return {"assignment_id": assignment_id, "patient_id": patient_id,
                "level_key": level_key, "level_name": level["level_name"],
                "reason": reason, "assessed_by": assessed_by, "assigned_at": now}

    def get_patient_care_level(self, patient_id: str) -> Optional[dict]:
        """获取患者当前护理等级(最新一条分配记录)"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT a.*, l.level_name, l.daily_fee FROM care_level_assignments a "
                "JOIN care_levels l ON a.level_key = l.level_key "
                "WHERE a.patient_id = ? ORDER BY a.assigned_at DESC LIMIT 1",
                (patient_id,)
            ).fetchone()
        return dict(row) if row else None

    # ================================================================
    # 交接班
    # ================================================================
    def create_handover(self, data: dict) -> dict:
        handover_id = self._gen_id("hov")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO handovers (handover_id, shift_from, shift_to, shift_type, "
                "patient_id, patient_name, situation, background, assessment, recommendation, "
                "pending_tasks, notes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (handover_id, data["shift_from"], data["shift_to"],
                 data.get("shift_type") or "day_to_night",
                 data.get("patient_id") or "", data.get("patient_name") or "",
                 data["situation"], data["background"],
                 data["assessment"], data["recommendation"],
                 data.get("pending_tasks") or "", data.get("notes") or "",
                 "pending", now),
            )
            conn.execute("COMMIT")
        return self.get_handover(handover_id)

    def get_handover(self, handover_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM handovers WHERE handover_id = ?", (handover_id,)).fetchone()
        return dict(row) if row else None

    def list_handovers(self, patient_id: Optional[str] = None, status: Optional[str] = None,
                       limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM handovers WHERE 1=1"
        params: list = []
        if patient_id:
            sql += " AND patient_id = ?"
            params.append(patient_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def acknowledge_handover(self, handover_id: str, acknowledged_by: str = "", note: str = "") -> Optional[dict]:
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE handovers SET status='acknowledged', acknowledged_at=?, notes=CASE WHEN notes='' THEN ? ELSE notes||'; '||? END "
                "WHERE handover_id = ? AND status='pending'",
                (now, note, note, handover_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return self.get_handover(handover_id)

    # ================================================================
    # 异常事件上报
    # ================================================================
    def create_incident(self, data: dict) -> dict:
        incident_id = self._gen_id("inc")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO incidents (incident_id, patient_id, patient_name, incident_type, "
                "severity, status, description, location, occurred_at, reporter, witnesses, "
                "immediate_action, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (incident_id, data["patient_id"], data.get("patient_name") or "",
                 data["incident_type"], data.get("severity") or "minor", "reported",
                 data["description"], data.get("location") or "",
                 data.get("occurred_at") or now, data.get("reporter") or "",
                 data.get("witnesses") or "", data.get("immediate_action") or "",
                 data.get("notes") or "", now, now),
            )
            conn.execute("COMMIT")
        return self.get_incident(incident_id)

    def get_incident(self, incident_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def list_incidents(self, patient_id: Optional[str] = None, severity: Optional[str] = None,
                       status: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM incidents WHERE 1=1"
        params: list = []
        if patient_id:
            sql += " AND patient_id = ?"
            params.append(patient_id)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def update_incident(self, incident_id: str, data: dict) -> Optional[dict]:
        fields = []
        params = []
        for key in ("severity", "status", "description", "follow_up", "root_cause",
                    "prevention", "resolved_by", "resolved_at", "notes"):
            if key in data and data[key] is not None:
                fields.append(f"{key} = ?")
                params.append(data[key])
        if not fields:
            return self.get_incident(incident_id)
        fields.append("updated_at = ?")
        params.append(self._now())
        params.append(incident_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(f"UPDATE incidents SET {', '.join(fields)} WHERE incident_id = ?", params)
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return self.get_incident(incident_id)

    def get_incident_stats(self, days: int = 30) -> dict:
        """异常事件统计"""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as c FROM incidents WHERE created_at >= ?", (cutoff,)
            ).fetchone()["c"]
            by_severity = {}
            for row in conn.execute(
                "SELECT severity, COUNT(*) as c FROM incidents WHERE created_at >= ? GROUP BY severity", (cutoff,)
            ).fetchall():
                by_severity[row["severity"]] = row["c"]
            by_type = {}
            for row in conn.execute(
                "SELECT incident_type, COUNT(*) as c FROM incidents WHERE created_at >= ? GROUP BY incident_type", (cutoff,)
            ).fetchall():
                by_type[row["incident_type"]] = row["c"]
            by_status = {}
            for row in conn.execute(
                "SELECT status, COUNT(*) as c FROM incidents WHERE created_at >= ? GROUP BY status", (cutoff,)
            ).fetchall():
                by_status[row["status"]] = row["c"]
        return {"total": total, "by_severity": by_severity,
                "by_type": by_type, "by_status": by_status, "period": f"近{days}天"}


    def get_operations_overview(self) -> dict:
        """运营总览：养老院基础运营看板指标。"""
        with self._connect() as conn:
            total_beds = conn.execute("SELECT COUNT(*) AS c FROM beds").fetchone()["c"]
            occupied_beds = conn.execute(
                "SELECT COUNT(*) AS c FROM beds WHERE status = 'occupied'"
            ).fetchone()["c"]

            total_residents = conn.execute(
                "SELECT COUNT(*) AS c FROM admissions WHERE status = 'active'"
            ).fetchone()["c"]
            pending_admissions = conn.execute(
                "SELECT COUNT(*) AS c FROM admissions WHERE status NOT IN ('active', 'discharged', 'rejected')"
            ).fetchone()["c"]

            open_incidents = conn.execute(
                "SELECT COUNT(*) AS c FROM incidents WHERE status IN ('reported', 'processing')"
            ).fetchone()["c"]
            pending_handovers = conn.execute(
                "SELECT COUNT(*) AS c FROM handovers WHERE status = 'pending'"
            ).fetchone()["c"]

            today_care_records = conn.execute(
                "SELECT COUNT(*) AS c FROM care_records WHERE DATE(recorded_at) = DATE('now', 'localtime')"
            ).fetchone()["c"]

        occupancy_rate = round((occupied_beds / total_beds) * 100, 2) if total_beds else 0.0
        return {
            "total_beds": total_beds,
            "occupied_beds": occupied_beds,
            "occupancy_rate": occupancy_rate,
            "total_residents": total_residents,
            "pending_admissions": pending_admissions,
            "open_incidents": open_incidents,
            "pending_handovers": pending_handovers,
            "today_care_records": today_care_records,
        }

    # ================================================================
    # 护理记录留痕
    # ================================================================
    def create_care_record(self, data: dict) -> dict:
        record_id = self._gen_id("rec")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO care_records (record_id, patient_id, patient_name, record_type, "
                "content, vital_data, recorded_by, recorded_at, shift, related_event_id, notes, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (record_id, data["patient_id"], data.get("patient_name") or "",
                 data.get("record_type") or "observation", data["content"],
                 data.get("vital_data") or "", data.get("recorded_by") or "",
                 data.get("recorded_at") or now, data.get("shift") or "",
                 data.get("related_event_id") or "", data.get("notes") or "", now),
            )
            conn.execute("COMMIT")
        return self.get_care_record(record_id)

    def get_care_record(self, record_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM care_records WHERE record_id = ?", (record_id,)).fetchone()
        return dict(row) if row else None

    def list_care_records(self, patient_id: Optional[str] = None, record_type: Optional[str] = None,
                          shift: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM care_records WHERE 1=1"
        params: list = []
        if patient_id:
            sql += " AND patient_id = ?"
            params.append(patient_id)
        if record_type:
            sql += " AND record_type = ?"
            params.append(record_type)
        if shift:
            sql += " AND shift = ?"
            params.append(shift)
        sql += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ================================================================
    # 入住流程 (Admissions)
    # ================================================================

    def _add_timeline(self, conn, admission_id: str, action: str,
                      operator: str = "", detail: str = "") -> None:
        """向时间线表写入一条记录（需在事务内调用）"""
        conn.execute(
            "INSERT INTO admission_timeline (admission_id, timestamp, action, operator, detail) "
            "VALUES (?,?,?,?,?)",
            (admission_id, self._now(), action, operator, detail),
        )

    def create_admission(self, data: dict, operator: str = "") -> dict:
        admission_id = self._gen_id("adm")
        now = self._now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO admissions (admission_id, status, applicant_name, applicant_gender, "
                "applicant_age, applicant_id_card, applicant_phone, guardian_name, guardian_phone, "
                "guardian_relation, guardian_id_card, health_summary, care_needs, preferred_room_type, "
                "expected_admission_date, referral_source, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (admission_id, "inquiry",
                 data["applicant_name"], data.get("applicant_gender") or "",
                 data.get("applicant_age"), data.get("applicant_id_card") or "",
                 data.get("applicant_phone") or "",
                 data.get("guardian_name") or "", data.get("guardian_phone") or "",
                 data.get("guardian_relation") or "", data.get("guardian_id_card") or "",
                 data.get("health_summary") or "", data.get("care_needs") or "",
                 data.get("preferred_room_type") or "", data.get("expected_admission_date") or "",
                 data.get("referral_source") or "", data.get("notes") or "",
                 now, now),
            )
            self._add_timeline(conn, admission_id, "创建入住申请",
                               operator, f"申请人: {data['applicant_name']}")
            conn.execute("COMMIT")
        return self.get_admission(admission_id)

    def get_admission(self, admission_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM admissions WHERE admission_id = ?", (admission_id,)).fetchone()
        return dict(row) if row else None

    def list_admissions(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = "SELECT * FROM admissions WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # 允许通过 extra_fields 更新的列白名单（防 SQL 注入）
    _ADMISSION_MUTABLE_COLUMNS: frozenset = frozenset({
        "assessment_id", "assessed_level", "assessment_conclusion", "assessed_at", "assessed_by",
        "contract_id", "contract_signed_at",
        "payment_id", "payment_status", "paid_at",
        "patient_id", "bed_id", "bed_number", "care_level_key", "actual_admission_date",
        "discharge_date", "discharge_reason", "settlement_amount", "refund_amount",
        "notes",
    })

    def update_admission_status(self, admission_id: str, new_status: str,
                                operator: str = "", detail: str = "",
                                extra_fields: Optional[dict] = None) -> Optional[dict]:
        """更新入住申请状态，同时写入时间线。extra_fields 可追加更新其它字段（白名单校验）。"""
        fields = ["status = ?", "updated_at = ?"]
        params: list = [new_status, self._now()]
        if extra_fields:
            for k, v in extra_fields.items():
                if v is not None:
                    if k not in self._ADMISSION_MUTABLE_COLUMNS:
                        raise ValueError(f"不允许更新列 '{k}'，合法列: {sorted(self._ADMISSION_MUTABLE_COLUMNS)}")
                    fields.append(f"{k} = ?")
                    params.append(v)
        params.append(admission_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE admissions SET {', '.join(fields)} WHERE admission_id = ?", params
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None
            self._add_timeline(conn, admission_id, f"状态变更→{new_status}",
                               operator, detail)
            conn.execute("COMMIT")
        return self.get_admission(admission_id)

    # ── 评估 ──
    def create_assessment(self, admission_id: str, data: dict, operator: str = "") -> dict:
        assessment_id = self._gen_id("asm")
        now = self._now()
        assessment_date = data.get("assessment_date") or now[:10]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO assessments (assessment_id, admission_id, adl_score, cognitive_score, "
                "nutrition_score, fall_risk_score, pressure_ulcer_risk, recommended_level, "
                "conclusion, special_needs, assessor, assessment_date, approved, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (assessment_id, admission_id,
                 data.get("adl_score"), data.get("cognitive_score"),
                 data.get("nutrition_score"), data.get("fall_risk_score"),
                 data.get("pressure_ulcer_risk"),
                 data["recommended_level"], data["conclusion"],
                 data.get("special_needs") or "",
                 data.get("assessor") or operator,
                 assessment_date,
                 1 if data.get("approved", True) else 0,
                 now),
            )
            # 更新主表冗余字段
            new_status = "assessed" if data.get("approved", True) else "inquiry"
            conn.execute(
                "UPDATE admissions SET status=?, assessment_id=?, assessed_level=?, "
                "assessment_conclusion=?, assessed_at=?, assessed_by=?, updated_at=? "
                "WHERE admission_id=?",
                (new_status, assessment_id, data["recommended_level"],
                 data["conclusion"], assessment_date,
                 data.get("assessor") or operator, now, admission_id),
            )
            action_detail = (f"评估{'通过' if data.get('approved', True) else '未通过'}: "
                             f"建议等级={data['recommended_level']}")
            self._add_timeline(conn, admission_id, "评估完成", operator, action_detail)
            conn.execute("COMMIT")
        return self.get_assessment(assessment_id)

    def get_assessment(self, assessment_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM assessments WHERE assessment_id = ?", (assessment_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["approved"] = bool(d.get("approved", 1))
        return d

    def get_assessments_by_admission(self, admission_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM assessments WHERE admission_id = ? ORDER BY created_at DESC",
                (admission_id,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["approved"] = bool(d.get("approved", 1))
            result.append(d)
        return result

    # ── 合同 ──
    def create_contract(self, admission_id: str, data: dict, operator: str = "") -> dict:
        contract_id = self._gen_id("ctr")
        now = self._now()
        # 生成合同编号: CTR-YYYYMMDD-XXXX
        contract_number = f"CTR-{now[:10].replace('-', '')}-{uuid.uuid4().hex[:4].upper()}"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO contracts (contract_id, admission_id, contract_type, contract_number, "
                "start_date, end_date, care_level_key, monthly_fee, deposit, payment_cycle, "
                "service_scope, special_terms, signed_by_guardian, signed_by_institution, "
                "signed_at, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (contract_id, admission_id,
                 data.get("contract_type") or "standard", contract_number,
                 data["start_date"], data.get("end_date") or "",
                 data["care_level_key"], data["monthly_fee"],
                 data.get("deposit") or 0, data.get("payment_cycle") or "monthly",
                 data.get("service_scope") or "", data.get("special_terms") or "",
                 data.get("signed_by_guardian") or "", data.get("signed_by_institution") or operator,
                 now, "active", now),
            )
            # 更新主表
            conn.execute(
                "UPDATE admissions SET status='contracted', contract_id=?, contract_signed_at=?, "
                "updated_at=? WHERE admission_id=?",
                (contract_id, now, now, admission_id),
            )
            self._add_timeline(conn, admission_id, "合同签署",
                               operator, f"合同号={contract_number}, 月费={data['monthly_fee']}")
            conn.execute("COMMIT")
        return self.get_contract(contract_id)

    def get_contract(self, contract_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
        return dict(row) if row else None

    def get_contracts_by_admission(self, admission_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM contracts WHERE admission_id = ? ORDER BY created_at DESC",
                (admission_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 缴费 ──
    def create_payment(self, admission_id: str, data: dict, operator: str = "") -> dict:
        payment_id = self._gen_id("pay")
        now = self._now()
        # 获取关联的合同ID
        admission = self.get_admission(admission_id)
        contract_id = admission.get("contract_id", "") if admission else ""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO payments (payment_id, admission_id, contract_id, payment_type, "
                "amount, payment_method, receipt_number, period_start, period_end, "
                "payer, notes, status, paid_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (payment_id, admission_id, contract_id,
                 data.get("payment_type") or "deposit",
                 data["amount"], data.get("payment_method") or "cash",
                 data.get("receipt_number") or "",
                 data.get("period_start") or "", data.get("period_end") or "",
                 data.get("payer") or "", data.get("notes") or "",
                 "completed", now, now),
            )
            # 更新主表
            conn.execute(
                "UPDATE admissions SET status='paid', payment_id=?, payment_status='completed', "
                "paid_at=?, updated_at=? WHERE admission_id=?",
                (payment_id, now, now, admission_id),
            )
            self._add_timeline(conn, admission_id, "缴费完成",
                               operator, f"金额={data['amount']}, 方式={data.get('payment_method', 'cash')}")
            conn.execute("COMMIT")
        return self.get_payment(payment_id)

    def get_payment(self, payment_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
        return dict(row) if row else None

    def get_payments_by_admission(self, admission_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM payments WHERE admission_id = ? ORDER BY created_at DESC",
                (admission_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── 办理入住（原子操作：床位分配 + 状态更新在同一事务内） ──
    def move_in(self, admission_id: str, bed_id: str, care_level_key: Optional[str] = None,
                patient_id: Optional[str] = None, admission_date: Optional[str] = None,
                operator: str = "") -> Optional[dict]:
        """办理入住：在单一事务内完成床位分配 + 入住状态更新，保证原子性。"""
        now = self._now()
        admission = self.get_admission(admission_id)
        if not admission:
            return None
        # 生成 patient_id (如果没有提供)
        if not patient_id:
            patient_id = self._gen_id("P")
        # 确定护理等级(优先参数 > 合同 > 评估)
        if not care_level_key:
            if admission.get("contract_id"):
                contract = self.get_contract(admission["contract_id"])
                if contract:
                    care_level_key = contract.get("care_level_key", "")
            if not care_level_key:
                care_level_key = admission.get("assessed_level", "")
        actual_date = admission_date or now[:10]
        patient_name = admission.get("applicant_name", "")

        # 原子事务：床位分配 + 入住状态更新
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. 释放该患者之前占用的床位
            conn.execute(
                "UPDATE beds SET status='available', patient_id='', patient_name='', assigned_at='', updated_at=? "
                "WHERE patient_id = ?",
                (now, patient_id),
            )
            # 2. 分配目标床位（仅当 available/reserved）
            cur = conn.execute(
                "UPDATE beds SET status='occupied', patient_id=?, patient_name=?, assigned_at=?, updated_at=? "
                "WHERE bed_id = ? AND status IN ('available', 'reserved')",
                (patient_id, patient_name, now, now, bed_id),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK")
                return None  # 床位不可用
            # 获取床位号
            bed_row = conn.execute("SELECT bed_number FROM beds WHERE bed_id = ?", (bed_id,)).fetchone()
            bed_number = bed_row["bed_number"] if bed_row else ""
            # 3. 更新入住状态
            conn.execute(
                "UPDATE admissions SET status='active', patient_id=?, bed_id=?, bed_number=?, "
                "care_level_key=?, actual_admission_date=?, updated_at=? WHERE admission_id=?",
                (patient_id, bed_id, bed_number, care_level_key, actual_date, now, admission_id),
            )
            self._add_timeline(conn, admission_id, "办理入住",
                               operator, f"床位={bed_number}, 等级={care_level_key}")
            conn.execute("COMMIT")

        # 分配护理等级（非关键路径，失败不阻断）
        if care_level_key:
            try:
                self.assign_care_level(patient_id, care_level_key,
                                       reason="入住评估", assessed_by=operator)
            except ValueError:
                pass  # 等级不存在时不阻断入住
        return self.get_admission(admission_id)

    # ── 离院（原子操作：状态更新 + 床位释放 + 财务数据在同一事务内） ──
    def discharge(self, admission_id: str, data: dict, operator: str = "") -> Optional[dict]:
        now = self._now()
        admission = self.get_admission(admission_id)
        if not admission:
            return None
        bed_id = admission.get("bed_id") or ""
        discharge_date = data.get("discharge_date") or now[:10]
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. 更新入住状态 + 持久化离院财务数据
            conn.execute(
                "UPDATE admissions SET status='discharged', discharge_date=?, discharge_reason=?, "
                "settlement_amount=?, refund_amount=?, updated_at=? WHERE admission_id=?",
                (discharge_date, data.get("discharge_reason") or "",
                 data.get("settlement_amount"), data.get("refund_amount"),
                 now, admission_id),
            )
            # 2. 在同一事务内释放床位
            if bed_id:
                conn.execute(
                    "UPDATE beds SET status='available', patient_id='', patient_name='', "
                    "assigned_at='', updated_at=? WHERE bed_id = ?",
                    (now, bed_id),
                )
            # 3. 时间线
            detail_parts = []
            if data.get("discharge_reason"):
                detail_parts.append(f"原因={data['discharge_reason']}")
            if data.get("settlement_amount") is not None:
                detail_parts.append(f"结算={data['settlement_amount']}")
            if data.get("refund_amount") is not None:
                detail_parts.append(f"退费={data['refund_amount']}")
            self._add_timeline(conn, admission_id, "办理离院",
                               operator, "; ".join(detail_parts) or "正常离院")
            conn.execute("COMMIT")
        return self.get_admission(admission_id)

    # ── 时间线 ──
    def get_admission_timeline(self, admission_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT timestamp, action, operator, detail FROM admission_timeline "
                "WHERE admission_id = ? ORDER BY id ASC",
                (admission_id,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── 全局 singleton 工厂 ────────────────────────────────────
_care_store_singleton: Optional[CareStore] = None
_care_store_lock = threading.Lock()


def get_care_store(db_path: str | Path | None = None) -> CareStore:
    """获取全局 CareStore 实例（线程安全惰性单例）。"""
    global _care_store_singleton
    if _care_store_singleton is not None:
        return _care_store_singleton
    with _care_store_lock:
        if _care_store_singleton is not None:
            return _care_store_singleton
        if db_path is None:
            from app.core.config import BASE_DIR
            care_dir = Path(BASE_DIR) / "local_care"
            care_dir.mkdir(parents=True, exist_ok=True)
            db_path = care_dir / "care.db"
        _care_store_singleton = CareStore(db_path)
    return _care_store_singleton


def reset_care_store() -> None:
    """仅测试用"""
    global _care_store_singleton
    with _care_store_lock:
        _care_store_singleton = None

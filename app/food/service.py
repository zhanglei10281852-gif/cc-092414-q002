from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin TEXT NOT NULL,
    harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL,
    trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL,
    collector TEXT NOT NULL,
    location TEXT NOT NULL,
    sample_weight_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected' CHECK(status IN ('collected','in_lab','complete','void')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_rule_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_code TEXT NOT NULL UNIQUE,
    standard_name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','retired')),
    published_at TEXT,
    retired_at TEXT,
    created_by TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_limit_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES food_rule_versions(id) ON DELETE RESTRICT,
    category TEXT NOT NULL,
    analyte TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT '',
    limit_mg_kg REAL NOT NULL CHECK(limit_mg_kg >= 0),
    unit TEXT NOT NULL DEFAULT 'mg/kg',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(version_id, category, analyte, method)
);
CREATE TABLE IF NOT EXISTS food_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES food_samples(id) ON DELETE RESTRICT,
    analyte TEXT NOT NULL,
    method TEXT NOT NULL,
    value_mg_kg REAL NOT NULL,
    limit_mg_kg REAL,
    unit TEXT NOT NULL,
    lab_operator TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    certificate_no TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail','review')),
    rule_id INTEGER REFERENCES food_limit_rules(id) ON DELETE RESTRICT,
    rule_version_code TEXT NOT NULL DEFAULT '',
    rule_snapshot_json TEXT NOT NULL DEFAULT '{}',
    result_hash TEXT NOT NULL,
    submission_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE IF NOT EXISTS food_result_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL REFERENCES food_test_results(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('pass','fail')),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    shipment_code TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    arrival_due_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_transit','arrived','delayed','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES food_shipments(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    source TEXT NOT NULL,
    in_range INTEGER NOT NULL CHECK(in_range IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(shipment_id, recorded_at)
);
CREATE TABLE IF NOT EXISTS food_risk_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('release','hold','recall','destroy')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    rule_version_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_samples_lot ON food_samples(lot_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_results_version ON food_test_results(rule_version_code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_food_results_submission ON food_test_results(submission_hash) WHERE submission_hash != '';
CREATE INDEX IF NOT EXISTS idx_food_rules_version ON food_limit_rules(version_id);
CREATE INDEX IF NOT EXISTS idx_food_rules_match ON food_limit_rules(category, analyte);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate_legacy_results(connection: sqlite3.Connection) -> None:
    """基线版本的结果表 verdict 不含 review、缺少规则快照列，先加列再重建。"""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(food_test_results)").fetchall()}
    for name, statement in {
        "limit_mg_kg": "ALTER TABLE food_test_results ADD COLUMN limit_mg_kg REAL",
        "rule_id": "ALTER TABLE food_test_results ADD COLUMN rule_id INTEGER",
        "rule_version_code": "ALTER TABLE food_test_results ADD COLUMN rule_version_code TEXT NOT NULL DEFAULT ''",
        "rule_snapshot_json": "ALTER TABLE food_test_results ADD COLUMN rule_snapshot_json TEXT NOT NULL DEFAULT '{}'",
        "submission_hash": "ALTER TABLE food_test_results ADD COLUMN submission_hash TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            connection.execute(statement)
    connection.executescript(
        """
        CREATE TABLE food_test_results_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id INTEGER NOT NULL REFERENCES food_samples(id) ON DELETE RESTRICT,
            analyte TEXT NOT NULL,
            method TEXT NOT NULL,
            value_mg_kg REAL NOT NULL,
            limit_mg_kg REAL,
            unit TEXT NOT NULL,
            lab_operator TEXT NOT NULL,
            tested_at TEXT NOT NULL,
            certificate_no TEXT NOT NULL DEFAULT '',
            verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail','review')),
            rule_id INTEGER,
            rule_version_code TEXT NOT NULL DEFAULT '',
            rule_snapshot_json TEXT NOT NULL DEFAULT '{}',
            result_hash TEXT NOT NULL,
            submission_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(sample_id, analyte, method, tested_at)
        );
        INSERT INTO food_test_results_new
            SELECT id,sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,certificate_no,
                   verdict,rule_id,rule_version_code,rule_snapshot_json,result_hash,submission_hash,created_at
            FROM food_test_results;
        DROP TABLE food_test_results;
        ALTER TABLE food_test_results_new RENAME TO food_test_results;
        CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
        CREATE INDEX IF NOT EXISTS idx_food_results_version ON food_test_results(rule_version_code);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_food_results_submission ON food_test_results(submission_hash) WHERE submission_hash != '';
        """
    )


def ensure_schema() -> None:
    connection = get_connection()
    # 必须先迁移基线旧表，再执行 SCHEMA：新索引依赖 rule_version_code 列。
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='food_test_results'"
    ).fetchone()
    if row is not None and "review" not in (row[0] or ""):
        _migrate_legacy_results(connection)
    connection.executescript(SCHEMA)
    risk_columns = {row["name"] for row in connection.execute("PRAGMA table_info(food_risk_actions)").fetchall()}
    if "rule_version_code" not in risk_columns:
        connection.execute(
            "ALTER TABLE food_risk_actions ADD COLUMN rule_version_code TEXT NOT NULL DEFAULT ''"
        )


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# 在当前生效版本中按产品类别 + 检测物匹配限值；同一检测物显式方法优先于通配方法（method=''）。
RULE_MATCH_SQL = """
SELECT r.id AS rule_id, r.category, r.analyte, r.method, r.limit_mg_kg, r.unit, r.note,
       v.id AS version_id, v.version_code, v.standard_name, v.published_at
FROM food_limit_rules r
JOIN food_rule_versions v ON v.id = r.version_id
WHERE v.status = 'active'
  AND r.category = ?
  AND r.analyte = ?
  AND (r.method = '' OR r.method = ?)
ORDER BY CASE WHEN r.method = ? THEN 0 ELSE 1 END, r.id
LIMIT 1
"""


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_lot(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["lot_code"], payload["product_name"], payload["category"], payload["supplier"], payload["origin"], payload["harvest_date"], payload["quantity_kg"], payload["trace_code"], now, now),
            )
            lot_id = cursor.lastrowid
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "lot.create", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def get_lot(self, lot_id: int, details: bool = True) -> dict[str, Any] | None:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return None
        result = dict(lot)
        if details:
            samples = self.connection.execute("SELECT * FROM food_samples WHERE lot_id=? ORDER BY collected_at,id", (lot_id,)).fetchall()
            shipments = self.connection.execute("SELECT * FROM food_shipments WHERE lot_id=? ORDER BY departure_at,id", (lot_id,)).fetchall()
            result["samples"] = []
            for sample in samples:
                item = dict(sample)
                item["results"] = [dict(row) for row in self.connection.execute("SELECT * FROM food_test_results WHERE sample_id=? ORDER BY tested_at,id", (sample["id"],)).fetchall()]
                result["samples"].append(item)
            result["shipments"] = [dict(row) for row in shipments]
        return result

    def add_sample(self, lot_id: int, payload: dict[str, Any], actor: str = "inspector") -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise KeyError("lot_not_found")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_samples(lot_id,sample_code,collected_at,collector,location,sample_weight_g,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (lot_id, payload["sample_code"], payload["collected_at"], payload["collector"], payload["location"], payload["sample_weight_g"], "collected", now))
            connection.execute("UPDATE food_lots SET status='testing',version=version+1,updated_at=? WHERE id=? AND status='pending'", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "sample.collect", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_samples WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    @staticmethod
    def _submission_hash(sample_id: int, payload: dict[str, Any]) -> str:
        return _result_hash({
            "kind": "food_test_submission",
            "sample_id": sample_id,
            "analyte": payload["analyte"],
            "method": payload["method"],
            "value_mg_kg": payload["value_mg_kg"],
            "tested_at": payload["tested_at"],
            "certificate_no": payload.get("certificate_no", ""),
        })

    def _hold_lot(self, connection: sqlite3.Connection, lot_id: int, reason: str, operator: str, version_code: str, now: str) -> None:
        """超限结果推动批次风险状态变化；终态（召回/销毁）不被降级，并留存处置记录。"""
        lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        previous_status = lot["status"]
        if previous_status in {"recalled", "destroyed"}:
            new_status = previous_status
        else:
            new_status = "held"
            connection.execute(
                "UPDATE food_lots SET risk_level='high',status='held',version=version+1,updated_at=? WHERE id=?",
                (now, lot_id),
            )
        connection.execute(
            "INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,rule_version_code,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (lot_id, "hold", reason, operator, previous_status, new_status, version_code, now),
        )

    def add_result(self, sample_id: int, payload: dict[str, Any], actor: str = "lab") -> dict[str, Any]:
        now = _now()
        submission_hash = self._submission_hash(sample_id, payload)
        with transaction(immediate=True) as connection:
            sample = connection.execute("SELECT * FROM food_samples WHERE id=?", (sample_id,)).fetchone()
            if sample is None:
                raise KeyError("sample_not_found")
            lot_id = sample["lot_id"]

            # 同一实验结果重复提交：自然键冲突或内容哈希一致时，直接返回既有结论，不生成第二条。
            existing = connection.execute(
                "SELECT * FROM food_test_results WHERE sample_id=? AND analyte=? AND method=? AND tested_at=?",
                (sample_id, payload["analyte"], payload["method"], payload["tested_at"]),
            ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT * FROM food_test_results WHERE submission_hash=?",
                    (submission_hash,),
                ).fetchone()
            if existing is not None:
                row = dict(existing)
                row["deduplicated"] = True
                return row

            lot = connection.execute("SELECT category FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            method = payload.get("method", "")
            rule = connection.execute(
                RULE_MATCH_SQL,
                (lot["category"], payload["analyte"], method, method),
            ).fetchone()

            if rule is not None:
                limit = rule["limit_mg_kg"]
                verdict = "pass" if payload["value_mg_kg"] <= limit else "fail"
                rule_id = rule["rule_id"]
                version_code = rule["version_code"]
                snapshot = {
                    "version_code": rule["version_code"],
                    "standard_name": rule["standard_name"],
                    "published_at": rule["published_at"],
                    "category": rule["category"],
                    "analyte": rule["analyte"],
                    "method": rule["method"],
                    "limit_mg_kg": rule["limit_mg_kg"],
                    "unit": rule["unit"],
                }
            else:
                # 缺少适用规则：限值留空、进入待复核，不允许由上报端自报限值直接出结论。
                limit = None
                verdict = "review"
                rule_id = None
                version_code = ""
                snapshot = {"reason": "no_applicable_rule", "category": lot["category"], "analyte": payload["analyte"]}

            result_hash = _result_hash({
                **payload,
                "verdict": verdict,
                "sample_id": sample_id,
                "rule_id": rule_id,
                "rule_version_code": version_code,
                "snapshot": snapshot,
            })
            cursor = connection.execute(
                "INSERT INTO food_test_results(sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,certificate_no,verdict,rule_id,rule_version_code,rule_snapshot_json,result_hash,submission_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sample_id, payload["analyte"], method, payload["value_mg_kg"], limit, payload["unit"], payload["lab_operator"], payload["tested_at"], payload.get("certificate_no", ""), verdict, rule_id, version_code, json.dumps(snapshot, ensure_ascii=False), result_hash, submission_hash, now),
            )
            result_id = cursor.lastrowid

            sample_status = "in_lab" if verdict == "review" else "complete"
            connection.execute("UPDATE food_samples SET status=? WHERE id=? AND status != 'void'", (sample_status, sample_id))
            connection.execute("UPDATE food_lots SET status='testing',version=version+1,updated_at=? WHERE id=? AND status='pending'", (now, lot_id))

            if verdict == "fail":
                reason = f"检测结果#{result_id} {payload['analyte']} 超限（{version_code}，限值 {limit}{rule['unit']}）"
                self._hold_lot(connection, lot_id, reason, "system", version_code, now)

            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                (lot_id, "test.result", actor, json.dumps({**payload, "verdict": verdict, "rule_version_code": version_code}, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_test_results WHERE id=?", (result_id,)).fetchone()) or {}

    def resolve_review(self, result_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """复核缺少适用规则的结果：给出 pass/fail 终局结论，fail 时推动批次扣留。"""
        now = _now()
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM food_test_results WHERE id=?", (result_id,)).fetchone()
            if row is None:
                raise KeyError("result_not_found")
            if row["verdict"] != "review":
                raise ValueError("result_not_pending_review")
            decision = payload["decision"]
            connection.execute("UPDATE food_test_results SET verdict=? WHERE id=?", (decision, result_id))
            connection.execute(
                "INSERT INTO food_result_reviews(result_id,decision,operator,reason,created_at) VALUES(?,?,?,?,?)",
                (result_id, decision, payload["operator"], payload["reason"], now),
            )
            sample = connection.execute("SELECT lot_id FROM food_samples WHERE id=?", (row["sample_id"],)).fetchone()
            lot_id = sample["lot_id"]
            connection.execute("UPDATE food_samples SET status='complete' WHERE id=? AND status != 'void'", (row["sample_id"],))
            if decision == "fail":
                reason = f"检测结果#{result_id} {row['analyte']} 复核超限（无适用规则，人工复核）"
                self._hold_lot(connection, lot_id, reason, payload["operator"], "", now)
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                (lot_id, "test.review", payload["operator"], json.dumps({"result_id": result_id, **payload}, ensure_ascii=False), now),
            )
        return self.get_result(result_id)

    def get_result(self, result_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT r.*, s.lot_id, l.lot_code, l.category
               FROM food_test_results r
               JOIN food_samples s ON s.id = r.sample_id
               JOIN food_lots l ON l.id = s.lot_id
               WHERE r.id=?""",
            (result_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["reviews"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT * FROM food_result_reviews WHERE result_id=? ORDER BY id",
                (result_id,),
            ).fetchall()
        ]
        return result

    def list_pending_reviews(self, lot_id: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, s.lot_id, l.lot_code, l.category FROM food_test_results r "
            "JOIN food_samples s ON s.id=r.sample_id JOIN food_lots l ON l.id=s.lot_id "
            "WHERE r.verdict='review'"
        )
        params: list[Any] = []
        if lot_id is not None:
            sql += " AND s.lot_id=?"
            params.append(lot_id)
        sql += " ORDER BY r.tested_at, r.id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    # ---- 限值规则目录与版本管理 -------------------------------------------------

    def create_rule_version(self, payload: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO food_rule_versions(version_code,standard_name,note,status,created_by,created_at,updated_at) VALUES(?,?,?,'draft',?,?,?)",
                    (payload["version_code"], payload["standard_name"], payload.get("note", ""), actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("rule_version_exists") from exc
            version_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.create", actor, json.dumps(payload, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_rule_versions WHERE id=?", (version_id,)).fetchone()) or {}

    def list_rule_versions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM food_rule_versions ORDER BY created_at,id").fetchall()]

    def _get_version(self, connection: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM food_rule_versions WHERE version_code=?", (code,)).fetchone()

    def get_rule_version(self, code: str) -> dict[str, Any]:
        with transaction() as connection:
            version = self._get_version(connection, code)
            if version is None:
                raise KeyError("rule_version_not_found")
            result = dict(version)
            result["rules"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM food_limit_rules WHERE version_id=? ORDER BY category,analyte,method,id",
                    (version["id"],),
                ).fetchall()
            ]
            return result

    def add_limit_rule(self, code: str, payload: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            version = self._get_version(connection, code)
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "draft":
                raise ValueError("rule_version_not_editable")
            try:
                cursor = connection.execute(
                    "INSERT INTO food_limit_rules(version_id,category,analyte,method,limit_mg_kg,unit,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (version["id"], payload["category"], payload["analyte"], payload.get("method", ""), payload["limit_mg_kg"], payload.get("unit", "mg/kg"), payload.get("note", ""), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("limit_rule_exists") from exc
            rule_id = cursor.lastrowid
            connection.execute("UPDATE food_rule_versions SET updated_at=? WHERE id=?", (now, version["id"]))
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("limit_rule.add", actor, json.dumps({"version_code": code, **payload}, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_limit_rules WHERE id=?", (rule_id,)).fetchone()) or {}

    def publish_rule_version(self, code: str, actor: str = "admin") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            version = self._get_version(connection, code)
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "draft":
                raise ValueError("rule_version_not_draft")
            rule_count = connection.execute("SELECT COUNT(*) FROM food_limit_rules WHERE version_id=?", (version["id"],)).fetchone()[0]
            if rule_count == 0:
                raise ValueError("rule_version_without_rules")
            retired = [
                row["version_code"]
                for row in connection.execute("SELECT version_code FROM food_rule_versions WHERE status='active'").fetchall()
            ]
            connection.execute(
                "UPDATE food_rule_versions SET status='retired',retired_at=?,updated_at=? WHERE status='active'",
                (now, now),
            )
            connection.execute(
                "UPDATE food_rule_versions SET status='active',published_at=?,updated_at=? WHERE id=?",
                (now, now, version["id"]),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.publish", actor, json.dumps({"version_code": code, "retired": retired}, ensure_ascii=False), now),
            )
            result = dict(self._get_version(connection, code))
            result["retired_versions"] = retired
            return result

    def retire_rule_version(self, code: str, actor: str = "admin") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            version = self._get_version(connection, code)
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "active":
                raise ValueError("rule_version_not_active")
            connection.execute(
                "UPDATE food_rule_versions SET status='retired',retired_at=?,updated_at=? WHERE id=?",
                (now, now, version["id"]),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.retire", actor, json.dumps({"version_code": code}, ensure_ascii=False), now),
            )
            return dict(self._get_version(connection, code))

    def version_traceability(self, code: str) -> dict[str, Any]:
        """按规则版本回看：版本与限值条目、依据该版本判定的结果快照、受影响批次范围。"""
        with transaction() as connection:
            version = self._get_version(connection, code)
            if version is None:
                raise KeyError("rule_version_not_found")
            rules = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM food_limit_rules WHERE version_id=? ORDER BY category,analyte,method,id",
                    (version["id"],),
                ).fetchall()
            ]
            results = [
                dict(row)
                for row in connection.execute(
                    """SELECT r.id,r.sample_id,r.analyte,r.method,r.value_mg_kg,r.limit_mg_kg,r.unit,
                              r.verdict,r.rule_version_code,r.rule_snapshot_json,r.tested_at,r.created_at,
                              s.lot_id,l.lot_code,l.product_name,l.category
                       FROM food_test_results r
                       JOIN food_samples s ON s.id=r.sample_id
                       JOIN food_lots l ON l.id=s.lot_id
                       WHERE r.rule_version_code=?
                       ORDER BY r.tested_at,r.id""",
                    (code,),
                ).fetchall()
            ]
            for item in results:
                item["rule_snapshot_json"] = json.loads(item["rule_snapshot_json"] or "{}")
            lots = [
                dict(row)
                for row in connection.execute(
                    """SELECT l.id,l.lot_code,l.product_name,l.category,l.status,l.risk_level,
                              COUNT(r.id) AS result_count,
                              SUM(CASE WHEN r.verdict='fail' THEN 1 ELSE 0 END) AS fail_count,
                              MIN(r.tested_at) AS first_tested_at,
                              MAX(r.tested_at) AS last_tested_at
                       FROM food_lots l
                       JOIN food_samples s ON s.lot_id=l.id
                       JOIN food_test_results r ON r.sample_id=s.id
                       WHERE r.rule_version_code=?
                       GROUP BY l.id
                       ORDER BY l.id""",
                    (code,),
                ).fetchall()
            ]
            return {
                "version": dict(version),
                "rules": rules,
                "impact": {
                    "result_count": len(results),
                    "pass_count": sum(1 for item in results if item["verdict"] == "pass"),
                    "fail_count": sum(1 for item in results if item["verdict"] == "fail"),
                    "review_count": sum(1 for item in results if item["verdict"] == "review"),
                    "lot_count": len(lots),
                    "lots": lots,
                    "results": results,
                },
            }

    def create_shipment(self, lot_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        if lot["status"] in {"held", "recalled", "destroyed"}:
            raise ValueError("lot_not_releasable")
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise ValueError("temperature_range_invalid")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_shipments(lot_id,shipment_code,carrier,vehicle_no,departure_at,arrival_due_at,destination,target_temp_min,target_temp_max,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (lot_id, payload["shipment_code"], payload["carrier"], payload["vehicle_no"], payload["departure_at"], payload["arrival_due_at"], payload["destination"], payload["target_temp_min"], payload["target_temp_max"], now, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "shipment.plan", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_shipments WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_temperature(self, shipment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        shipment = self.connection.execute("SELECT * FROM food_shipments WHERE id=?", (shipment_id,)).fetchone()
        if shipment is None:
            raise KeyError("shipment_not_found")
        in_range = int(shipment["target_temp_min"] <= payload["temperature_c"] <= shipment["target_temp_max"])
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_temperatures WHERE shipment_id=? AND recorded_at=?", (shipment_id, payload["recorded_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_temperatures(shipment_id,recorded_at,temperature_c,source,in_range,created_at) VALUES(?,?,?,?,?,?)", (shipment_id, payload["recorded_at"], payload["temperature_c"], payload["source"], in_range, now))
            if not in_range:
                connection.execute("UPDATE food_shipments SET status='delayed',updated_at=? WHERE id=? AND status IN ('planned','in_transit')", (now, shipment_id))
            return _dict(connection.execute("SELECT * FROM food_temperatures WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def decide_risk(self, lot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise KeyError("lot_not_found")
            mapping = {"release": "released", "hold": "held", "recall": "recalled", "destroy": "destroyed"}
            new_status = mapping[payload["decision"]]
            now = _now()
            connection.execute("UPDATE food_lots SET status=?,version=version+1,updated_at=? WHERE id=?", (new_status, now, lot_id))
            connection.execute("INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)", (lot_id, payload["decision"], payload["reason"], payload["operator"], lot["status"], new_status, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "risk." + payload["decision"], payload["operator"], json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def summary(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id, details=False)
        if lot is None:
            raise KeyError("lot_not_found")
        sample_count = self.connection.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0]
        result_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        failed_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'", (lot_id,)).fetchone()[0]
        review_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='review'", (lot_id,)).fetchone()[0]
        temperature_count = self.connection.execute("SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments s ON s.id=t.shipment_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        return {"lot": lot, "sample_count": sample_count, "result_count": result_count, "failed_count": failed_count, "pending_review_count": review_count, "temperature_count": temperature_count}

    def delete_lot(self, lot_id: int) -> bool:
        """移除尚未关联记录的批次；关联记录的错误映射由上层负责。"""
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise KeyError("lot_not_found")
            return True

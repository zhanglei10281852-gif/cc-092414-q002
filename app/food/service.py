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
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','review','held','recalled','destroyed')),
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
    title TEXT NOT NULL,
    standard_ref TEXT NOT NULL DEFAULT '',
    change_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','deprecated')),
    created_by TEXT NOT NULL DEFAULT 'system',
    published_at TEXT,
    deprecated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_limit_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES food_rule_versions(id) ON DELETE RESTRICT,
    rule_code TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    analyte TEXT NOT NULL,
    limit_mg_kg REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT 'mg/kg',
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    created_at TEXT NOT NULL,
    UNIQUE(version_id, category, analyte)
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
    rule_id INTEGER REFERENCES food_limit_rules(id),
    rule_version_id INTEGER REFERENCES food_rule_versions(id),
    rule_version_code TEXT NOT NULL DEFAULT '',
    rule_category TEXT NOT NULL DEFAULT '',
    rule_analyte TEXT NOT NULL DEFAULT '',
    rule_standard_ref TEXT NOT NULL DEFAULT '',
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE IF NOT EXISTS food_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id INTEGER NOT NULL UNIQUE REFERENCES food_test_results(id) ON DELETE RESTRICT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
    requested_by TEXT NOT NULL,
    resolution TEXT NOT NULL DEFAULT '',
    resolved_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_food_rules_match ON food_limit_rules(category, analyte, status);
CREATE INDEX IF NOT EXISTS idx_food_reviews_lot ON food_reviews(lot_id, status);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
"""

_RESULT_COLUMNS = (
    "id,sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,"
    "certificate_no,verdict,rule_id,rule_version_id,rule_version_code,rule_category,"
    "rule_analyte,rule_standard_ref,result_hash,created_at"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    connection = get_connection()
    # 表重建（CHECK 约束 / 新增快照列）期间临时关闭外键，按 SQLite 官方迁移流程执行。
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.executescript(SCHEMA)
        _migrate(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_food_results_version ON food_test_results(rule_version_id)"
        )
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _migrate(connection: sqlite3.Connection) -> None:
    result_cols = {row[1] for row in connection.execute("PRAGMA table_info(food_test_results)")}
    if result_cols and "rule_id" not in result_cols:
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
    rule_id INTEGER REFERENCES food_limit_rules(id),
    rule_version_id INTEGER REFERENCES food_rule_versions(id),
    rule_version_code TEXT NOT NULL DEFAULT '',
    rule_category TEXT NOT NULL DEFAULT '',
    rule_analyte TEXT NOT NULL DEFAULT '',
    rule_standard_ref TEXT NOT NULL DEFAULT '',
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
INSERT INTO food_test_results_new
    (id,sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,
     certificate_no,verdict,result_hash,created_at)
SELECT id,sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,
       certificate_no,verdict,result_hash,created_at FROM food_test_results;
DROP TABLE food_test_results;
ALTER TABLE food_test_results_new RENAME TO food_test_results;
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_results_version ON food_test_results(rule_version_id);
"""
        )
    lot_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='food_lots'"
    ).fetchone()
    if lot_sql is not None and "'review'" not in (lot_sql[0] or ""):
        connection.executescript(
            """
CREATE TABLE food_lots_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin TEXT NOT NULL,
    harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL,
    trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','review','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO food_lots_new SELECT * FROM food_lots;
DROP TABLE food_lots;
ALTER TABLE food_lots_new RENAME TO food_lots;
"""
        )


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 规则目录

    def create_rule_version(self, payload: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO food_rule_versions(version_code,title,standard_ref,change_note,status,created_by,created_at,updated_at) VALUES(?,?,?,?, 'draft', ?,?,?)",
                    (payload["version_code"], payload["title"], payload.get("standard_ref", ""), payload.get("change_note", ""), actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("rule_version_code_exists") from exc
            version_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.create", actor, json.dumps(payload, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_rule_versions WHERE id=?", (version_id,)).fetchone()) or {}

    def list_rule_versions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM food_rule_versions ORDER BY created_at DESC,id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_rule_version(self, version_id: int) -> dict[str, Any] | None:
        version = self.connection.execute(
            "SELECT * FROM food_rule_versions WHERE id=?", (version_id,)
        ).fetchone()
        if version is None:
            return None
        result = dict(version)
        result["rules"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM food_limit_rules WHERE version_id=? ORDER BY category,analyte,id",
                (version_id,),
            ).fetchall()
        ]
        return result

    def add_limit_rule(self, version_id: int, payload: dict[str, Any], actor: str = "admin") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            version = connection.execute(
                "SELECT * FROM food_rule_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "draft":
                raise ValueError("rule_version_not_editable")
            now = _now()
            try:
                cursor = connection.execute(
                    "INSERT INTO food_limit_rules(version_id,rule_code,category,analyte,limit_mg_kg,unit,note,status,created_at) VALUES(?,?,?,?,?,?,?, 'active', ?)",
                    (version_id, payload["rule_code"], payload["category"].strip(), payload["analyte"].strip(), payload["limit_mg_kg"], payload.get("unit", "mg/kg"), payload.get("note", ""), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("limit_rule_conflict") from exc
            connection.execute(
                "UPDATE food_rule_versions SET updated_at=? WHERE id=?", (now, version_id)
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule.add", actor, json.dumps({**payload, "version_id": version_id}, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_limit_rules WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def publish_rule_version(self, version_id: int, actor: str = "admin", effective_at: str | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            version = connection.execute(
                "SELECT * FROM food_rule_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "draft":
                raise ValueError("rule_version_not_draft")
            rule_count = connection.execute(
                "SELECT COUNT(*) FROM food_limit_rules WHERE version_id=? AND status='active'",
                (version_id,),
            ).fetchone()[0]
            if rule_count == 0:
                raise ValueError("rule_version_without_rules")
            now = _now()
            published_at = effective_at or now
            connection.execute(
                "UPDATE food_rule_versions SET status='published',published_at=?,updated_at=? WHERE id=?",
                (published_at, now, version_id),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.publish", actor, json.dumps({"version_id": version_id, "published_at": published_at}, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_rule_versions WHERE id=?", (version_id,)).fetchone()) or {}

    def deprecate_rule_version(self, version_id: int, actor: str = "admin") -> dict[str, Any]:
        """停用规则版本：不再参与新判定，但已引用它的历史结果快照保持不变。"""
        with transaction(immediate=True) as connection:
            version = connection.execute(
                "SELECT * FROM food_rule_versions WHERE id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise KeyError("rule_version_not_found")
            if version["status"] != "published":
                raise ValueError("rule_version_not_published")
            now = _now()
            connection.execute(
                "UPDATE food_rule_versions SET status='deprecated',deprecated_at=?,updated_at=? WHERE id=?",
                (now, now, version_id),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("rule_version.deprecate", actor, json.dumps({"version_id": version_id}, ensure_ascii=False), now),
            )
            return _dict(connection.execute("SELECT * FROM food_rule_versions WHERE id=?", (version_id,)).fetchone()) or {}

    def _find_applicable_rule(
        self, connection: sqlite3.Connection, category: str, analyte: str, tested_at: str
    ) -> sqlite3.Row | None:
        """按产品类别+检测物匹配检测时刻已发布的最新有效版本中的限值规则。"""
        return connection.execute(
            """
            SELECT r.*, v.version_code AS v_code, v.standard_ref AS v_standard_ref, v.published_at AS v_published_at
            FROM food_limit_rules r
            JOIN food_rule_versions v ON v.id = r.version_id
            WHERE r.status='active' AND v.status='published'
              AND r.category=? AND r.analyte=? AND v.published_at<=?
            ORDER BY v.published_at DESC, v.id DESC
            LIMIT 1
            """,
            (category, analyte, tested_at),
        ).fetchone()

    # ------------------------------------------------------------------ 批次与检测

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
                item["results"] = [dict(row) for row in self.connection.execute(f"SELECT {_RESULT_COLUMNS} FROM food_test_results WHERE sample_id=? ORDER BY tested_at,id", (sample["id"],)).fetchall()]
                result["samples"].append(item)
            result["shipments"] = [dict(row) for row in shipments]
            result["pending_reviews"] = self.connection.execute(
                "SELECT COUNT(*) FROM food_reviews WHERE lot_id=? AND status='pending'", (lot_id,)
            ).fetchone()[0]
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

    def add_result(self, sample_id: int, payload: dict[str, Any], actor: str = "lab") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            sample = connection.execute("SELECT * FROM food_samples WHERE id=?", (sample_id,)).fetchone()
            if sample is None:
                raise KeyError("sample_not_found")
            lot_id = sample["lot_id"]

            existing = connection.execute(
                f"SELECT {_RESULT_COLUMNS} FROM food_test_results WHERE sample_id=? AND analyte=? AND method=? AND tested_at=?",
                (sample_id, payload["analyte"], payload["method"], payload["tested_at"]),
            ).fetchone()
            if existing:
                # 同一实验（样品+检测物+方法+检测时刻）重复提交：幂等返回原结论，不生成第二条。
                result = dict(existing)
                result["deduplicated"] = True
                return result

            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            rule = self._find_applicable_rule(connection, lot["category"], payload["analyte"].strip(), payload["tested_at"])

            if rule is None:
                # 缺少适用规则：不得自行判定，结果与批次进入待复核。
                verdict = "review"
                limit_value = None
                rule_id = rule_version_id = None
                version_code = rule_category = rule_analyte = standard_ref = ""
            else:
                limit_value = rule["limit_mg_kg"]
                verdict = "pass" if payload["value_mg_kg"] <= limit_value else "fail"
                rule_id = rule["id"]
                rule_version_id = rule["version_id"]
                version_code = rule["v_code"]
                rule_category = rule["category"]
                rule_analyte = rule["analyte"]
                standard_ref = rule["v_standard_ref"]

            result_hash = _result_hash({
                "sample_id": sample_id,
                "analyte": payload["analyte"],
                "method": payload["method"],
                "value_mg_kg": payload["value_mg_kg"],
                "unit": payload["unit"],
                "tested_at": payload["tested_at"],
                "certificate_no": payload.get("certificate_no", ""),
                "verdict": verdict,
                "rule_id": rule_id,
                "rule_version_code": version_code,
                "rule_limit_mg_kg": limit_value,
            })

            cursor = connection.execute(
                """
                INSERT INTO food_test_results
                    (sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,
                     certificate_no,verdict,rule_id,rule_version_id,rule_version_code,rule_category,
                     rule_analyte,rule_standard_ref,result_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (sample_id, payload["analyte"], payload["method"], payload["value_mg_kg"], limit_value,
                 payload["unit"], payload["lab_operator"], payload["tested_at"],
                 payload.get("certificate_no", ""), verdict, rule_id, rule_version_id, version_code,
                 rule_category, rule_analyte, standard_ref, result_hash, now),
            )
            result_id = cursor.lastrowid
            connection.execute("UPDATE food_samples SET status='complete' WHERE id=?", (sample_id,))

            if rule is None:
                connection.execute(
                    "INSERT INTO food_reviews(result_id,lot_id,reason,status,requested_by,created_at) VALUES(?,?,?, 'pending', ?,?)",
                    (result_id, lot_id, "no_applicable_rule", payload["lab_operator"], now),
                )
                connection.execute(
                    "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, "test.review_required", actor, json.dumps({"result_id": result_id, "analyte": payload["analyte"], "reason": "no_applicable_rule"}, ensure_ascii=False), now),
                )
            else:
                connection.execute(
                    "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (lot_id, "test.result", actor, json.dumps({**payload, "verdict": verdict, "rule_version_code": version_code, "rule_limit_mg_kg": limit_value}, ensure_ascii=False), now),
                )

            self._recompute_lot_status(connection, lot_id, now)
            return _dict(connection.execute(f"SELECT {_RESULT_COLUMNS} FROM food_test_results WHERE id=?", (result_id,)).fetchone()) or {}

    @staticmethod
    def _recompute_lot_status(connection: sqlite3.Connection, lot_id: int, now: str) -> None:
        """根据已登记结论重算批次风险：超限一律扣留；否则有待复核则挂起。

        held/review 为无条件收敛，即使批次已放行，补录的风险结果也会把它拉回管控。
        """
        failed = connection.execute(
            "SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'",
            (lot_id,),
        ).fetchone()[0]
        if failed:
            connection.execute(
                "UPDATE food_lots SET risk_level='high',status='held',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('held','recalled','destroyed')",
                (now, lot_id),
            )
            return
        pending_reviews = connection.execute(
            "SELECT COUNT(*) FROM food_reviews WHERE lot_id=? AND status='pending'", (lot_id,)
        ).fetchone()[0]
        if pending_reviews:
            # 已放行批次补录无规则结果同样拉回复核；held/召回/销毁状态优先级更高。
            connection.execute(
                "UPDATE food_lots SET status='review',version=version+1,updated_at=? WHERE id=? AND status IN ('pending','testing','released','review')",
                (now, lot_id),
            )

    # ------------------------------------------------------------------ 待复核

    def list_reviews(self, lot_id: int) -> list[dict[str, Any]]:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise KeyError("lot_not_found")
        rows = self.connection.execute(
            """
            SELECT w.*, r.analyte, r.value_mg_kg, r.unit, r.tested_at, r.verdict
            FROM food_reviews w JOIN food_test_results r ON r.id=w.result_id
            WHERE w.lot_id=? ORDER BY w.created_at,w.id
            """,
            (lot_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_review(self, review_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """人工复核无规则结果：判定变更全程留痕，批次状态随之更新。"""
        now = _now()
        with transaction(immediate=True) as connection:
            review = connection.execute("SELECT * FROM food_reviews WHERE id=?", (review_id,)).fetchone()
            if review is None:
                raise KeyError("review_not_found")
            if review["status"] != "pending":
                raise ValueError("review_already_resolved")
            resolution = payload["resolution"]
            result_id = review["result_id"]
            lot_id = review["lot_id"]
            connection.execute(
                "UPDATE food_reviews SET status='resolved',resolution=?,resolved_by=?,resolved_at=? WHERE id=?",
                (resolution, payload["operator"], now, review_id),
            )
            connection.execute(
                "UPDATE food_test_results SET verdict=? WHERE id=?",
                ("fail" if resolution == "fail" else "pass", result_id),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                (lot_id, "review.resolve", payload["operator"], json.dumps({"review_id": review_id, "result_id": result_id, **payload}, ensure_ascii=False), now),
            )
            self._recompute_lot_status(connection, lot_id, now)
            if resolution != "fail":
                # 复核通过且无其他未决风险/待复核：回到检测中，重新走放行决策。
                connection.execute(
                    """
                    UPDATE food_lots SET status='testing',updated_at=?
                    WHERE id=? AND status='review'
                      AND NOT EXISTS (SELECT 1 FROM food_reviews WHERE lot_id=? AND status='pending')
                    """,
                    (now, lot_id, lot_id),
                )
            return _dict(connection.execute("SELECT * FROM food_reviews WHERE id=?", (review_id,)).fetchone()) or {}

    def get_result_basis(self, result_id: int) -> dict[str, Any] | None:
        """回看一条结果当时的判定依据：结果行内固化的规则快照 + 版本当前状态。"""
        row = self.connection.execute(
            f"SELECT {_RESULT_COLUMNS} FROM food_test_results WHERE id=?", (result_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        version = None
        rule = None
        if result["rule_version_id"] is not None:
            version = _dict(self.connection.execute(
                "SELECT * FROM food_rule_versions WHERE id=?", (result["rule_version_id"],)
            ).fetchone())
            rule = _dict(self.connection.execute(
                "SELECT * FROM food_limit_rules WHERE id=?", (result["rule_id"],)
            ).fetchone())
        review = _dict(self.connection.execute(
            "SELECT * FROM food_reviews WHERE result_id=?", (result_id,)
        ).fetchone())
        return {"result": result, "rule": rule, "version": version, "review": review}

    def version_impact(self, version_id: int) -> dict[str, Any] | None:
        """按规则版本回看判定依据与受影响批次范围。"""
        version = self.get_rule_version(version_id)
        if version is None:
            return None
        results = [
            dict(row)
            for row in self.connection.execute(
                f"""
                SELECT rr.*, s.sample_code, l.id AS lot_id, l.lot_code, l.product_name
                FROM (SELECT {_RESULT_COLUMNS} FROM food_test_results) rr
                JOIN food_samples s ON s.id=rr.sample_id
                JOIN food_lots l ON l.id=s.lot_id
                WHERE rr.rule_version_id=?
                ORDER BY rr.tested_at,rr.id
                """,
                (version_id,),
            ).fetchall()
        ]
        affected_lots = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT l.id AS lot_id, l.lot_code, l.category, l.product_name, l.status, l.risk_level,
                       SUM(CASE WHEN r.verdict='fail' THEN 1 ELSE 0 END) AS fail_count,
                       SUM(CASE WHEN r.verdict='pass' THEN 1 ELSE 0 END) AS pass_count,
                       COUNT(*) AS result_count,
                       MIN(r.tested_at) AS first_tested_at,
                       MAX(r.tested_at) AS last_tested_at
                FROM food_test_results r
                JOIN food_samples s ON s.id=r.sample_id
                JOIN food_lots l ON l.id=s.lot_id
                WHERE r.rule_version_id=?
                GROUP BY l.id
                ORDER BY l.id
                """,
                (version_id,),
            ).fetchall()
        ]
        return {
            "version": version,
            "results": results,
            "affected_lots": affected_lots,
            "result_count": len(results),
            "lot_count": len(affected_lots),
        }

    # ------------------------------------------------------------------ 运输与风险

    def create_shipment(self, lot_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        if lot["status"] in {"review", "held", "recalled", "destroyed"}:
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
            if payload["decision"] == "release":
                pending = connection.execute(
                    "SELECT COUNT(*) FROM food_reviews WHERE lot_id=? AND status='pending'", (lot_id,)
                ).fetchone()[0]
                if pending:
                    raise ValueError("lot_pending_review")
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
        review_count = self.connection.execute("SELECT COUNT(*) FROM food_reviews WHERE lot_id=? AND status='pending'", (lot_id,)).fetchone()[0]
        temperature_count = self.connection.execute("SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments s ON s.id=t.shipment_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        return {"lot": lot, "sample_count": sample_count, "result_count": result_count, "failed_count": failed_count, "pending_review_count": review_count, "temperature_count": temperature_count}

    def delete_lot(self, lot_id: int) -> bool:
        """移除尚未关联记录的批次；关联记录的错误映射由上层负责。"""
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise KeyError("lot_not_found")
            return True

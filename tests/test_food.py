from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient


def lot(client, code="LOT-001", category="叶菜"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": category, "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def publish_rules(client, code, rules, effective_at="2026-01-01T00:00:00+00:00", title=None):
    created = client.post("/api/food/rule-versions", json={"version_code": code, "title": title or code, "standard_ref": "GB 2763"})
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    for rule in rules:
        resp = client.post(f"/api/food/rule-versions/{version_id}/rules", json=rule)
        assert resp.status_code == 201, resp.text
    published = client.post(f"/api/food/rule-versions/{version_id}/publish", json={"effective_at": effective_at})
    assert published.status_code == 200, published.text
    return version_id


def collect_sample(client, lot_id, code="S-001"):
    sample = client.post(f"/api/food/lots/{lot_id}/samples", json={"sample_code": code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250})
    assert sample.status_code == 201, sample.text
    return sample.json()


RESULT_BODY = {
    "analyte": "毒死蜱",
    "method": "GB/T 5009",
    "value_mg_kg": 0.02,
    "lab_operator": "实验员",
    "tested_at": "2026-09-21T18:00:00+00:00",
}


def test_food_chain_and_risk_flow(client):
    publish_rules(client, "GB2763-2024", [
        {"rule_code": "R-YE-DSC", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.05},
    ])
    created = lot(client)
    sample = collect_sample(client, created["id"])
    result = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY)
    assert result.status_code == 201
    body = result.json()
    assert body["verdict"] == "pass" and body["limit_mg_kg"] == 0.05
    assert body["rule_version_code"] == "GB2763-2024"
    shipment = client.post(f"/api/food/lots/{created['id']}/shipments", json={"shipment_code": "SHIP-001", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
    assert shipment.status_code == 201
    temp = client.post(f"/api/food/shipments/{shipment.json()['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201 and temp.json()["in_range"] == 0
    decision = client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "release", "reason": "检测合格且已复核", "operator": "监管员"})
    assert decision.status_code == 200 and decision.json()["status"] == "released"


def test_failed_residue_holds_lot(client):
    publish_rules(client, "GB2763-2024", [
        {"rule_code": "R-YE-LQJZ", "category": "叶菜", "analyte": "氯氰菊酯", "limit_mg_kg": 0.05},
    ])
    created = lot(client, "LOT-002")
    sample = collect_sample(client, created["id"], "S-002")
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={**RESULT_BODY, "analyte": "氯氰菊酯", "value_mg_kg": 0.3})
    assert result.json()["verdict"] == "fail"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "held" and detail["risk_level"] == "high"


def test_rule_version_lifecycle_and_immutable_after_publish(client):
    created = client.post("/api/food/rule-versions", json={"version_code": "DRAFT-1", "title": "草稿"})
    assert created.status_code == 201 and created.json()["status"] == "draft"
    version_id = created.json()["id"]
    empty = client.post(f"/api/food/rule-versions/{version_id}/publish", json={})
    assert empty.status_code == 409
    add = client.post(f"/api/food/rule-versions/{version_id}/rules", json={"rule_code": "R-1", "category": "叶菜", "analyte": "敌敌畏", "limit_mg_kg": 0.1})
    assert add.status_code == 201
    duplicate = client.post(f"/api/food/rule-versions/{version_id}/rules", json={"rule_code": "R-2", "category": "叶菜", "analyte": "敌敌畏", "limit_mg_kg": 0.2})
    assert duplicate.status_code == 409
    published = client.post(f"/api/food/rule-versions/{version_id}/publish", json={"effective_at": "2026-01-01T00:00:00+00:00"}).json()
    assert published["status"] == "published" and published["published_at"].startswith("2026-01-01")
    locked = client.post(f"/api/food/rule-versions/{version_id}/rules", json={"rule_code": "R-3", "category": "根茎", "analyte": "敌敌畏", "limit_mg_kg": 0.1})
    assert locked.status_code == 409
    republish = client.post(f"/api/food/rule-versions/{version_id}/publish", json={})
    assert republish.status_code == 409
    deprecated = client.post(f"/api/food/rule-versions/{version_id}/deprecate")
    assert deprecated.status_code == 200 and deprecated.json()["status"] == "deprecated"
    assert client.post(f"/api/food/rule-versions/{version_id}/deprecate").status_code == 409


def test_missing_rule_sends_result_and_lot_to_review(client):
    created = lot(client, "LOT-REVIEW")
    sample = collect_sample(client, created["id"], "S-RV")
    result = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY)
    assert result.status_code == 201
    body = result.json()
    assert body["verdict"] == "review" and body["limit_mg_kg"] is None and body["rule_version_id"] is None
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "review" and detail["pending_reviews"] == 1
    # 待复核批次不能安排运输或放行
    blocked_shipment = client.post(f"/api/food/lots/{created['id']}/shipments", json={"shipment_code": "SHIP-X", "carrier": "冷链物流", "vehicle_no": "鲁A002", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
    assert blocked_shipment.status_code == 409
    blocked_release = client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "release", "reason": "尝试放行", "operator": "监管员"})
    assert blocked_release.status_code == 409

    reviews = client.get(f"/api/food/lots/{created['id']}/reviews").json()
    assert len(reviews) == 1 and reviews[0]["reason"] == "no_applicable_rule"
    resolved = client.post(f"/api/food/reviews/{reviews[0]['id']}/resolve", json={"resolution": "pass", "operator": "复核员", "note": "参照内部限值合格"})
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "testing" and detail["pending_reviews"] == 0
    basis = client.get(f"/api/food/results/{body['id']}/basis").json()
    assert basis["result"]["verdict"] == "pass" and basis["review"]["status"] == "resolved"
    assert client.post(f"/api/food/reviews/{reviews[0]['id']}/resolve", json={"resolution": "fail", "operator": "复核员"}).status_code == 409


def test_review_resolved_as_fail_holds_lot(client):
    created = lot(client, "LOT-RV-FAIL")
    sample = collect_sample(client, created["id"], "S-RVF")
    body = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY).json()
    assert body["verdict"] == "review"
    review = client.get(f"/api/food/lots/{created['id']}/reviews").json()[0]
    response = client.post(f"/api/food/reviews/{review['id']}/resolve", json={"resolution": "fail", "operator": "复核员", "note": "复核超限"})
    assert response.status_code == 200
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "held" and detail["risk_level"] == "high"


def test_duplicate_result_submission_is_idempotent(client):
    publish_rules(client, "GB2763-2024", [
        {"rule_code": "R-YE-DSC", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.05},
    ])
    created = lot(client, "LOT-DUP")
    sample = collect_sample(client, created["id"], "S-DUP")
    first = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY).json()
    second = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY).json()
    assert second["id"] == first["id"] and second["deduplicated"] is True
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert len(detail["samples"][0]["results"]) == 1


def test_historical_snapshot_survives_new_rule_version(client):
    v1 = publish_rules(client, "GB2763-2021", [
        {"rule_code": "R-YE-DSC-OLD", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.05},
    ], effective_at="2021-09-01T00:00:00+00:00")
    created = lot(client, "LOT-SNAP")
    sample = collect_sample(client, created["id"], "S-SNAP")
    historical = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY).json()
    assert historical["verdict"] == "pass"
    assert historical["rule_version_id"] == v1 and historical["limit_mg_kg"] == 0.05

    # 收紧限值并停用旧版本：0.02 在新版下本应超限，但历史结论与快照不变
    client.post(f"/api/food/rule-versions/{v1}/deprecate")
    publish_rules(client, "GB2763-2026", [
        {"rule_code": "R-YE-DSC-NEW", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.01},
    ], effective_at="2026-01-01T00:00:00+00:00")
    basis = client.get(f"/api/food/results/{historical['id']}/basis").json()
    assert basis["result"]["verdict"] == "pass"
    assert basis["result"]["rule_version_code"] == "GB2763-2021"
    assert basis["result"]["limit_mg_kg"] == 0.05
    assert basis["rule"]["limit_mg_kg"] == 0.05
    assert basis["version"]["status"] == "deprecated"

    # 同一样品的新检测按新版判定为超限
    new_body = {**RESULT_BODY, "tested_at": "2026-09-23T18:00:00+00:00", "certificate_no": "C-2"}
    newer = client.post(f"/api/food/samples/{sample['id']}/results", json=new_body).json()
    assert newer["verdict"] == "fail" and newer["rule_version_code"] == "GB2763-2026"


def test_effective_date_selects_version_at_test_time(client):
    publish_rules(client, "V-OLD", [
        {"rule_code": "OLD", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.1},
    ], effective_at="2020-01-01T00:00:00+00:00")
    publish_rules(client, "V-NEW", [
        {"rule_code": "NEW", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.01},
    ], effective_at="2026-06-01T00:00:00+00:00")
    created = lot(client, "LOT-TIME")
    sample = collect_sample(client, created["id"], "S-TIME")
    before = client.post(f"/api/food/samples/{sample['id']}/results", json={**RESULT_BODY, "tested_at": "2026-05-01T00:00:00+00:00"}).json()
    assert before["rule_version_code"] == "V-OLD" and before["verdict"] == "pass"
    after = client.post(f"/api/food/samples/{sample['id']}/results", json={**RESULT_BODY, "value_mg_kg": 0.02, "tested_at": "2026-07-01T00:00:00+00:00"}).json()
    assert after["rule_version_code"] == "V-NEW" and after["verdict"] == "fail"


def test_multiple_pending_reviews_keep_lot_in_review(client):
    created = lot(client, "LOT-RV2")
    sample = collect_sample(client, created["id"], "S-RV2")
    first = client.post(f"/api/food/samples/{sample['id']}/results", json=RESULT_BODY).json()
    second = client.post(f"/api/food/samples/{sample['id']}/results", json={**RESULT_BODY, "analyte": "克百威", "tested_at": "2026-09-21T19:00:00+00:00"}).json()
    assert first["verdict"] == "review" and second["verdict"] == "review"
    reviews = client.get(f"/api/food/lots/{created['id']}/reviews").json()
    assert len(reviews) == 2
    client.post(f"/api/food/reviews/{reviews[0]['id']}/resolve", json={"resolution": "pass", "operator": "复核员"})
    # 仍有另一条待复核：批次保持挂起
    assert client.get(f"/api/food/lots/{created['id']}").json()["status"] == "review"
    client.post(f"/api/food/reviews/{reviews[1]['id']}/resolve", json={"resolution": "pass", "operator": "复核员"})
    assert client.get(f"/api/food/lots/{created['id']}").json()["status"] == "testing"


def test_version_impact_lists_basis_and_affected_lots(client):
    version_id = publish_rules(client, "GB2763-2024", [
        {"rule_code": "R-YE-DSC", "category": "叶菜", "analyte": "毒死蜱", "limit_mg_kg": 0.05},
        {"rule_code": "R-YE-LQJZ", "category": "叶菜", "analyte": "氯氰菊酯", "limit_mg_kg": 0.05},
    ])
    lot_a = lot(client, "LOT-IMP-A")
    sample_a = collect_sample(client, lot_a["id"], "S-IMP-A")
    client.post(f"/api/food/samples/{sample_a['id']}/results", json=RESULT_BODY)
    lot_b = lot(client, "LOT-IMP-B", category="叶菜")
    sample_b = collect_sample(client, lot_b["id"], "S-IMP-B")
    client.post(f"/api/food/samples/{sample_b['id']}/results", json={**RESULT_BODY, "analyte": "氯氰菊酯", "value_mg_kg": 0.3})

    impact = client.get(f"/api/food/rule-versions/{version_id}/impact").json()
    assert impact["result_count"] == 2 and impact["lot_count"] == 2
    codes = {item["lot_code"] for item in impact["affected_lots"]}
    assert codes == {"LOT-IMP-A", "LOT-IMP-B"}
    held = {item["lot_code"]: item for item in impact["affected_lots"]}["LOT-IMP-B"]
    assert held["fail_count"] == 1 and held["status"] == "held"
    assert client.get("/api/food/rule-versions/9999/impact").status_code == 404


def test_legacy_database_migrates_with_history_intact(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(db_path))
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
CREATE TABLE food_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lot_code TEXT NOT NULL UNIQUE, product_name TEXT NOT NULL,
    category TEXT NOT NULL, supplier TEXT NOT NULL, origin TEXT NOT NULL, harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL, trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE food_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INTEGER NOT NULL, sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL, collector TEXT NOT NULL, location TEXT NOT NULL, sample_weight_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected', created_at TEXT NOT NULL
);
CREATE TABLE food_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL, analyte TEXT NOT NULL, method TEXT NOT NULL,
    value_mg_kg REAL NOT NULL, limit_mg_kg REAL NOT NULL, unit TEXT NOT NULL, lab_operator TEXT NOT NULL,
    tested_at TEXT NOT NULL, certificate_no TEXT NOT NULL DEFAULT '', verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
    result_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE food_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id INTEGER, action TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
"""
    )
    conn.execute(
        "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,status,risk_level,created_at,updated_at) VALUES('LOT-OLD','菠菜','叶菜','农场','山东','2026-09-01',100,'LOT-OLD-T','pending','unknown','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO food_samples(lot_id,sample_code,collected_at,collector,location,sample_weight_g,created_at) VALUES(1,'S-OLD','2026-09-02T08:00:00+00:00','监管员','市场',200,'2026-09-02T08:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO food_test_results(sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,verdict,result_hash,created_at) VALUES(1,'毒死蜱','GB/T 5009',0.02,0.05,'mg/kg','实验员','2026-09-02T18:00:00+00:00','pass','legacy','2026-09-02T18:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as test_client:
        detail = test_client.get("/api/food/lots/1").json()
        assert detail["lot_code"] == "LOT-OLD"
        old_result = detail["samples"][0]["results"][0]
        assert old_result["verdict"] == "pass" and old_result["rule_version_id"] is None
        # 迁移后新流程可用：无规则结果进入待复核，批次状态机接受 review
        review_result = test_client.post("/api/food/samples/1/results", json={
            "analyte": "未知检测物", "method": "GB/T 5009", "value_mg_kg": 1.0,
            "lab_operator": "实验员", "tested_at": "2026-09-03T18:00:00+00:00",
        }).json()
        assert review_result["verdict"] == "review"
        assert test_client.get("/api/food/lots/1").json()["status"] == "review"
    close_connection()

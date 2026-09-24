from __future__ import annotations


def lot(client, code="LOT-001", category="叶菜"):
    response = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": category, "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"})
    assert response.status_code == 201, response.text
    return response.json()


def sample(client, lot_id, code="S-001", location="批发市场"):
    response = client.post(f"/api/food/lots/{lot_id}/samples", json={"sample_code": code, "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": location, "sample_weight_g": 250})
    assert response.status_code == 201, response.text
    return response.json()


def result_payload(**overrides):
    payload = {"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": 0.02, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"}
    payload.update(overrides)
    return payload


def publish_version(client, code, rules, standard_name="食品中农药最大残留限量"):
    created = client.post("/api/food/rule-versions", json={"version_code": code, "standard_name": standard_name, "note": "测试版本"})
    assert created.status_code == 201, created.text
    for rule in rules:
        added = client.post(f"/api/food/rule-versions/{code}/rules", json=rule)
        assert added.status_code == 201, added.text
    published = client.post(f"/api/food/rule-versions/{code}/publish")
    assert published.status_code == 200, published.text
    return published.json()


def test_food_chain_and_risk_flow(client):
    created = lot(client)
    s = sample(client, created["id"])
    # 没有生效规则：结果进入待复核，但不阻断后续运输与人工风险处置流程。
    result = client.post(f"/api/food/samples/{s['id']}/results", json=result_payload())
    assert result.status_code == 201 and result.json()["verdict"] == "review"
    shipment = client.post(f"/api/food/lots/{created['id']}/shipments", json={"shipment_code": "SHIP-001", "carrier": "冷链物流", "vehicle_no": "鲁A001", "departure_at": "2026-09-22T01:00:00+00:00", "arrival_due_at": "2026-09-22T10:00:00+00:00", "destination": "市民餐桌", "target_temp_min": 0, "target_temp_max": 8})
    assert shipment.status_code == 201
    temp = client.post(f"/api/food/shipments/{shipment.json()['id']}/temperatures", json={"recorded_at": "2026-09-22T04:00:00+00:00", "temperature_c": 12, "source": "sensor-A"})
    assert temp.status_code == 201 and temp.json()["in_range"] == 0
    decision = client.post(f"/api/food/lots/{created['id']}/risk", json={"decision": "release", "reason": "检测合格且已复核", "operator": "监管员"})
    assert decision.status_code == 200 and decision.json()["status"] == "released"


def test_failed_residue_holds_lot(client):
    publish_version(client, "GB2763-2021", [{"category": "叶菜", "analyte": "氯氰菊酯", "method": "", "limit_mg_kg": 0.05}])
    created = lot(client, "LOT-002")
    s = sample(client, created["id"], "S-002", "农贸市场")
    result = client.post(f"/api/food/samples/{s['id']}/results", json=result_payload(analyte="氯氰菊酯", value_mg_kg=0.3))
    body = result.json()
    assert body["verdict"] == "fail" and body["limit_mg_kg"] == 0.05 and body["rule_version_code"] == "GB2763-2021"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "held" and detail["risk_level"] == "high"


def test_rule_match_by_category_analyte_and_snapshot(client):
    publish_version(client, "V1", [{"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.05}])
    created = lot(client, "LOT-003")
    s = sample(client, created["id"], "S-003")
    result = client.post(f"/api/food/samples/{s['id']}/results", json=result_payload(value_mg_kg=0.05)).json()
    assert result["verdict"] == "pass" and result["rule_id"]
    import json

    snapshot = json.loads(result["rule_snapshot_json"])
    assert snapshot["version_code"] == "V1" and snapshot["limit_mg_kg"] == 0.05

    # 换版后历史结论保持不变：限值、判定、快照都还停留在 V1。
    publish_version(client, "V2", [{"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.01}])
    historical = client.get(f"/api/food/results/{result['id']}").json()
    assert historical["verdict"] == "pass"
    assert historical["limit_mg_kg"] == 0.05
    assert historical["rule_version_code"] == "V1"

    # 同样的测量值按新版规则则超限，推动批次扣留。
    s2 = sample(client, created["id"], "S-004")
    new_result = client.post(f"/api/food/samples/{s2['id']}/results", json=result_payload(value_mg_kg=0.02, tested_at="2026-09-22T18:00:00+00:00")).json()
    assert new_result["verdict"] == "fail" and new_result["rule_version_code"] == "V2" and new_result["limit_mg_kg"] == 0.01
    assert client.get(f"/api/food/lots/{created['id']}").json()["status"] == "held"


def test_method_specific_rule_takes_precedence(client):
    publish_version(client, "V1", [
        {"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.05},
        {"category": "叶菜", "analyte": "毒死蜱", "method": "GB/T 5009", "limit_mg_kg": 0.1},
    ])
    created = lot(client, "LOT-004")
    s = sample(client, created["id"], "S-005")
    specific = client.post(f"/api/food/samples/{s['id']}/results", json=result_payload(method="GB/T 5009", value_mg_kg=0.08)).json()
    assert specific["verdict"] == "pass" and specific["limit_mg_kg"] == 0.1
    s2 = sample(client, created["id"], "S-006")
    wildcard = client.post(f"/api/food/samples/{s2['id']}/results", json=result_payload(method="NY/T 761", value_mg_kg=0.08, tested_at="2026-09-22T18:00:00+00:00")).json()
    assert wildcard["verdict"] == "fail" and wildcard["limit_mg_kg"] == 0.05


def test_missing_rule_enters_review_and_resolution_holds_lot(client):
    created = lot(client, "LOT-005")
    s = sample(client, created["id"], "S-007")
    result = client.post(f"/api/food/samples/{s['id']}/results", json=result_payload(analyte="未知农残X", value_mg_kg=9.9)).json()
    assert result["verdict"] == "review" and result["limit_mg_kg"] is None and result["rule_id"] is None
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert detail["status"] == "testing"  # 待复核期间不直接扣留
    pending = client.get("/api/food/results/pending-review").json()
    assert len(pending) == 1 and pending[0]["id"] == result["id"]

    reviewed = client.post(f"/api/food/results/{result['id']}/review", json={"decision": "fail", "operator": "复核员", "reason": "参照限量临时判定超限"}).json()
    assert reviewed["verdict"] == "fail"
    assert reviewed["reviews"][0]["operator"] == "复核员"
    assert client.get(f"/api/food/lots/{created['id']}").json()["status"] == "held"

    # 已终局的结果不能重复复核。
    again = client.post(f"/api/food/results/{result['id']}/review", json={"decision": "pass", "operator": "复核员", "reason": "x"})
    assert again.status_code == 409


def test_duplicate_submission_does_not_create_second_conclusion(client):
    publish_version(client, "V1", [{"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.05}])
    created = lot(client, "LOT-006")
    s = sample(client, created["id"], "S-008")
    payload = result_payload()
    first = client.post(f"/api/food/samples/{s['id']}/results", json=payload).json()

    # 规则换版后重复提交同一实验结果：仍然只认既有结论，不按新版重判。
    publish_version(client, "V2", [{"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.01}])
    second = client.post(f"/api/food/samples/{s['id']}/results", json=payload).json()
    assert second["id"] == first["id"] and second["deduplicated"] is True
    assert second["verdict"] == "pass" and second["rule_version_code"] == "V1"
    detail = client.get(f"/api/food/lots/{created['id']}").json()
    assert len(detail["samples"][0]["results"]) == 1


def test_version_lifecycle_guards(client):
    created = client.post("/api/food/rule-versions", json={"version_code": "DRAFT-1", "standard_name": "草稿"}).json()
    # 空版本不能发布。
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/publish").status_code == 409
    client.post(f"/api/food/rule-versions/{created['version_code']}/rules", json={"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.05})
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/publish").status_code == 200
    # 生效版本不能继续加条目，也不能重复发布。
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/rules", json={"category": "叶菜", "analyte": "a", "limit_mg_kg": 1}).status_code == 409
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/publish").status_code == 409
    # 停用后无生效版本，新结果进入待复核。
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/retire").status_code == 200
    assert client.post(f"/api/food/rule-versions/{created['version_code']}/retire").status_code == 409
    created_lot = lot(client, "LOT-007")
    s = sample(client, created_lot["id"], "S-009")
    assert client.post(f"/api/food/samples/{s['id']}/results", json=result_payload()).json()["verdict"] == "review"
    # 同版本编码不能重复创建。
    assert client.post("/api/food/rule-versions", json={"version_code": "DRAFT-1", "standard_name": "x"}).status_code == 409


def test_version_traceability_reports_basis_and_impacted_lots(client):
    publish_version(client, "V1", [{"category": "叶菜", "analyte": "毒死蜱", "method": "", "limit_mg_kg": 0.05}])
    first_lot = lot(client, "LOT-010")
    s1 = sample(client, first_lot["id"], "S-010")
    client.post(f"/api/food/samples/{s1['id']}/results", json=result_payload(value_mg_kg=0.02))
    second_lot = lot(client, "LOT-011", category="叶菜")
    s2 = sample(client, second_lot["id"], "S-011")
    client.post(f"/api/food/samples/{s2['id']}/results", json=result_payload(value_mg_kg=0.2, tested_at="2026-09-22T09:00:00+00:00"))

    report = client.get("/api/food/rule-versions/V1/traceability").json()
    assert report["version"]["version_code"] == "V1"
    assert len(report["rules"]) == 1
    impact = report["impact"]
    assert impact["result_count"] == 2 and impact["pass_count"] == 1 and impact["fail_count"] == 1
    assert impact["lot_count"] == 2
    impacted_codes = {item["lot_code"] for item in impact["lots"]}
    assert impacted_codes == {"LOT-010", "LOT-011"}
    failed_lot = next(item for item in impact["lots"] if item["lot_code"] == "LOT-011")
    assert failed_lot["fail_count"] == 1
    assert impact["results"][0]["rule_snapshot_json"]["limit_mg_kg"] == 0.05

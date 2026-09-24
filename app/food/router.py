from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.food.schemas import (
    LimitRuleCreate,
    LotCreate,
    ReviewResolve,
    RiskDecision,
    RuleVersionCreate,
    RuleVersionPublish,
    SampleCreate,
    ShipmentCreate,
    TemperatureRecord,
    TestResultCreate,
)
from app.food.service import FoodService

router = APIRouter(prefix="/api/food", tags=["食品安全"])


def service() -> FoodService:
    return FoodService()


def _conflict(exc: ValueError) -> HTTPException:
    messages = {
        "rule_version_code_exists": "规则版本号已存在",
        "rule_version_not_editable": "规则版本已发布或停用，不可修改",
        "rule_version_not_draft": "仅草稿版本可发布",
        "rule_version_not_published": "仅已发布版本可停用",
        "rule_version_without_rules": "规则版本没有任何有效限值，不能发布",
        "limit_rule_conflict": "该类别与检测物的限值规则在本版本中已存在",
        "review_already_resolved": "该待复核项已处理",
        "lot_not_releasable": "批次处于待复核或风险管控状态，不能放行",
        "lot_pending_review": "批次存在待复核检测结果，不能放行",
        "temperature_range_invalid": "温度区间下限不能高于上限",
    }
    return HTTPException(status_code=409, detail=messages.get(str(exc), str(exc)))


# ------------------------------------------------------------------ 规则目录


@router.post("/rule-versions", status_code=201)
def create_rule_version(payload: RuleVersionCreate):
    try:
        return service().create_rule_version(payload.model_dump())
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.get("/rule-versions")
def list_rule_versions():
    return service().list_rule_versions()


@router.get("/rule-versions/{version_id}")
def get_rule_version(version_id: int):
    value = service().get_rule_version(version_id)
    if value is None:
        raise HTTPException(status_code=404, detail="规则版本不存在")
    return value


@router.post("/rule-versions/{version_id}/rules", status_code=201)
def add_limit_rule(version_id: int, payload: LimitRuleCreate):
    try:
        return service().add_limit_rule(version_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则版本不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/rule-versions/{version_id}/publish", status_code=200)
def publish_rule_version(version_id: int, payload: RuleVersionPublish | None = None):
    effective_at = payload.effective_at if payload else None
    try:
        return service().publish_rule_version(version_id, effective_at=effective_at)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则版本不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/rule-versions/{version_id}/deprecate", status_code=200)
def deprecate_rule_version(version_id: int):
    try:
        return service().deprecate_rule_version(version_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="规则版本不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.get("/rule-versions/{version_id}/impact")
def rule_version_impact(version_id: int):
    value = service().version_impact(version_id)
    if value is None:
        raise HTTPException(status_code=404, detail="规则版本不存在")
    return value


@router.get("/results/{result_id}/basis")
def result_basis(result_id: int):
    value = service().get_result_basis(result_id)
    if value is None:
        raise HTTPException(status_code=404, detail="检测结果不存在")
    return value


# ------------------------------------------------------------------ 批次


@router.post("/lots", status_code=201)
def create_lot(payload: LotCreate):
    try:
        return service().create_lot(payload.model_dump(), actor=payload.supplier)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="批次编码或追溯码已存在") from exc
        raise


@router.get("/lots/{lot_id}")
def get_lot(lot_id: int, details: bool = True):
    value = service().get_lot(lot_id, details)
    if value is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return value


@router.get("/lots/{lot_id}/summary")
def summary(lot_id: int):
    try:
        return service().summary(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: int):
    try:
        service().delete_lot(lot_id)
        return {"message": "批次已删除"}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/lots/{lot_id}/samples", status_code=201)
def add_sample(lot_id: int, payload: SampleCreate):
    try:
        return service().add_sample(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.get("/lots/{lot_id}/reviews")
def list_reviews(lot_id: int):
    try:
        return service().list_reviews(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/reviews/{review_id}/resolve", status_code=200)
def resolve_review(review_id: int, payload: ReviewResolve):
    try:
        return service().resolve_review(review_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="待复核项不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/samples/{sample_id}/results", status_code=201)
def add_result(sample_id: int, payload: TestResultCreate):
    try:
        return service().add_result(sample_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="样品不存在") from exc


@router.post("/lots/{lot_id}/shipments", status_code=201)
def create_shipment(lot_id: int, payload: ShipmentCreate):
    try:
        return service().create_shipment(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/shipments/{shipment_id}/temperatures", status_code=201)
def add_temperature(shipment_id: int, payload: TemperatureRecord):
    try:
        return service().add_temperature(shipment_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运输单不存在") from exc


@router.post("/lots/{lot_id}/risk", status_code=200)
def decide_risk(lot_id: int, payload: RiskDecision):
    try:
        return service().decide_risk(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    except ValueError as exc:
        raise _conflict(exc) from exc

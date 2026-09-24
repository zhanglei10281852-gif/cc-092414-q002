from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.food.schemas import (
    LimitRuleCreate,
    LotCreate,
    ReviewDecision,
    RiskDecision,
    RuleVersionCreate,
    SampleCreate,
    ShipmentCreate,
    TemperatureRecord,
    TestResultCreate,
)
from app.food.service import FoodService

router = APIRouter(prefix="/api/food", tags=["食品安全"])


def service() -> FoodService:
    return FoodService()


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


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
        raise _not_found("批次不存在")
    return value


@router.get("/lots/{lot_id}/summary")
def summary(lot_id: int):
    try:
        return service().summary(lot_id)
    except KeyError as exc:
        raise _not_found("批次不存在") from exc


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: int):
    try:
        service().delete_lot(lot_id)
        return {"message": "批次已删除"}
    except KeyError as exc:
        raise _not_found("批次不存在") from exc


@router.post("/lots/{lot_id}/samples", status_code=201)
def add_sample(lot_id: int, payload: SampleCreate):
    try:
        return service().add_sample(lot_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("批次不存在") from exc


@router.post("/samples/{sample_id}/results", status_code=201)
def add_result(sample_id: int, payload: TestResultCreate):
    try:
        return service().add_result(sample_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("样品不存在") from exc


@router.get("/results/pending-review")
def list_pending_reviews(lot_id: int | None = None):
    return service().list_pending_reviews(lot_id)


@router.get("/results/{result_id}")
def get_result(result_id: int):
    value = service().get_result(result_id)
    if value is None:
        raise _not_found("检测结果不存在")
    return value


@router.post("/results/{result_id}/review", status_code=200)
def resolve_review(result_id: int, payload: ReviewDecision):
    try:
        return service().resolve_review(result_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("检测结果不存在") from exc
    except ValueError as exc:
        raise _conflict(str(exc)) from exc


# ---- 限值规则目录与版本 ---------------------------------------------------------


@router.post("/rule-versions", status_code=201)
def create_rule_version(payload: RuleVersionCreate):
    try:
        return service().create_rule_version(payload.model_dump())
    except ValueError as exc:
        raise _conflict("规则版本编码已存在") from exc


@router.get("/rule-versions")
def list_rule_versions():
    return service().list_rule_versions()


@router.get("/rule-versions/{code}")
def get_rule_version(code: str):
    try:
        return service().get_rule_version(code)
    except KeyError as exc:
        raise _not_found("规则版本不存在") from exc


@router.post("/rule-versions/{code}/rules", status_code=201)
def add_limit_rule(code: str, payload: LimitRuleCreate):
    try:
        return service().add_limit_rule(code, payload.model_dump())
    except KeyError as exc:
        raise _not_found("规则版本不存在") from exc
    except ValueError as exc:
        if str(exc) == "limit_rule_exists":
            raise _conflict("该版本内类别、检测物与方法的限值条目已存在") from exc
        raise _conflict("仅草稿状态的规则版本可以维护限值条目") from exc


@router.post("/rule-versions/{code}/publish", status_code=200)
def publish_rule_version(code: str):
    try:
        return service().publish_rule_version(code)
    except KeyError as exc:
        raise _not_found("规则版本不存在") from exc
    except ValueError as exc:
        message = {
            "rule_version_not_draft": "仅草稿状态的规则版本可以发布",
            "rule_version_without_rules": "规则版本没有任何限值条目，不能发布",
        }.get(str(exc), str(exc))
        raise _conflict(message) from exc


@router.post("/rule-versions/{code}/retire", status_code=200)
def retire_rule_version(code: str):
    try:
        return service().retire_rule_version(code)
    except KeyError as exc:
        raise _not_found("规则版本不存在") from exc
    except ValueError as exc:
        raise _conflict("仅生效中的规则版本可以停用") from exc


@router.get("/rule-versions/{code}/traceability")
def version_traceability(code: str):
    try:
        return service().version_traceability(code)
    except KeyError as exc:
        raise _not_found("规则版本不存在") from exc


@router.post("/lots/{lot_id}/shipments", status_code=201)
def create_shipment(lot_id: int, payload: ShipmentCreate):
    try:
        return service().create_shipment(lot_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("批次不存在") from exc
    except ValueError as exc:
        raise _conflict(str(exc)) from exc


@router.post("/shipments/{shipment_id}/temperatures", status_code=201)
def add_temperature(shipment_id: int, payload: TemperatureRecord):
    try:
        return service().add_temperature(shipment_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("运输单不存在") from exc


@router.post("/lots/{lot_id}/risk", status_code=200)
def decide_risk(lot_id: int, payload: RiskDecision):
    try:
        return service().decide_risk(lot_id, payload.model_dump())
    except KeyError as exc:
        raise _not_found("批次不存在") from exc

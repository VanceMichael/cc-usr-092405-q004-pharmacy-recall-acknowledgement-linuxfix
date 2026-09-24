"""HTTP 边界：只做鉴权头解析、JSON 编解码与事务包裹，规则全部在 services。"""

from __future__ import annotations

from typing import Any, Callable

from flask import Blueprint, jsonify, request
from sqlalchemy.engine import Engine

from . import services as svc


def build_api(engine: Engine, transport: Callable[[dict[str, Any]], None] | None = None) -> Blueprint:
    api = Blueprint("api", __name__)

    def _body() -> dict[str, Any]:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise svc.DomainError("请求体必须是 JSON 对象")
        return data

    def _actor_id() -> str:
        actor_id = request.headers.get("X-Actor-Id")
        if not actor_id:
            raise svc.AuthError("缺少 X-Actor-Id")
        return actor_id

    # -------------------------------------------------------- 身份/关系登记
    @api.post("/admin/brands")
    def admin_brand():
        b = _body()
        with engine.begin() as conn:
            brand_id = svc.register_brand(conn, b["name"], brand_id=b.get("brand_id"), at=b.get("at"))
        return jsonify(brand_id=brand_id), 201

    @api.post("/admin/entities")
    def admin_entity():
        b = _body()
        with engine.begin() as conn:
            entity_id = svc.register_entity(
                conn, b["brand_id"], b["name"], entity_id=b.get("entity_id"), at=b.get("at"))
        return jsonify(entity_id=entity_id), 201

    @api.post("/admin/stores")
    def admin_store():
        b = _body()
        with engine.begin() as conn:
            store_id = svc.register_store(
                conn, b["brand_id"], b["name"], b["entry_type"],
                store_id=b.get("store_id"), at=b.get("at"))
        return jsonify(store_id=store_id), 201

    @api.post("/admin/actors")
    def admin_actor():
        b = _body()
        with engine.begin() as conn:
            svc.register_actor(
                conn, b["actor_id"], b["role"], brand_id=b.get("brand_id"),
                entity_id=b.get("entity_id"), store_id=b.get("store_id"), at=b.get("at"))
        return jsonify(actor_id=b["actor_id"]), 201

    @api.post("/admin/licenses")
    def admin_license():
        b = _body()
        with engine.begin() as conn:
            license_id = svc.upsert_license(
                conn, b["entity_id"], b["license_no"], b["valid_from"], b.get("valid_until"),
                recorded_at=b.get("at"))
        return jsonify(license_id=license_id), 201

    @api.post("/admin/relations")
    def admin_relation():
        b = _body()
        with engine.begin() as conn:
            relation_id = svc.put_relation(
                conn, b["brand_id"], b["store_id"], b["entity_id"], b["relation_type"],
                b["valid_from"], recorded_by=b["recorded_by"], recorded_at=b.get("at"))
        return jsonify(relation_id=relation_id), 201

    @api.post("/admin/supplies")
    def admin_supply():
        b = _body()
        with engine.begin() as conn:
            supply_id = svc.record_supply(
                conn, brand_id=b["brand_id"], store_id=b["store_id"],
                product_code=b["product_code"], product_name=b["product_name"],
                batch_no=b["batch_no"], supplied_at=b["supplied_at"],
                supplier_entity_id=b.get("supplier_entity_id"),
                quantity=b.get("quantity", 0), recorded_at=b.get("at"))
        return jsonify(supply_id=supply_id), 201

    # -------------------------------------------------------- 召回
    @api.post("/recalls")
    def create_recall():
        actor_id = _actor_id()
        b = _body()
        with engine.begin() as conn:
            result = svc.create_recall(
                conn, brand_id=b["brand_id"], product_code=b["product_code"],
                product_name=b["product_name"], batch_no=b["batch_no"],
                risk_level=b["risk_level"], effective_at=b["effective_at"],
                created_by=actor_id)
        return jsonify(result), 201

    @api.post("/recalls/<recall_id>/versions")
    def correct_batch(recall_id: str):
        actor_id = _actor_id()
        b = _body()
        with engine.begin() as conn:
            result = svc.correct_batch(
                conn, recall_id=recall_id, new_batch_no=b["batch_no"],
                reason=b.get("reason", "批号纠错"), created_by=actor_id)
        return jsonify(result), 201

    # -------------------------------------------------------- 回执/异议
    @api.post("/targets/<target_id>/receipts")
    def submit_receipt(target_id: str):
        actor_id = _actor_id()
        b = _body()
        with engine.begin() as conn:
            result = svc.submit_receipt(
                conn, actor_id=actor_id, target_id=target_id,
                device_id=b["device_id"], client_seq=int(b["client_seq"]),
                action=b["action"], action_at=b["action_at"])
        return jsonify(result), 200

    @api.post("/disputes/<dispute_id>/review")
    def review_dispute(dispute_id: str):
        actor_id = _actor_id()
        b = _body()
        with engine.begin() as conn:
            result = svc.review_dispute(
                conn, dispute_id=dispute_id, reviewer_actor_id=actor_id,
                verdict=b["verdict"], note=b.get("note", ""),
                to_entity_id=b.get("to_entity_id"))
        return jsonify(result), 200

    # -------------------------------------------------------- 运维：升级与补发
    @api.post("/ticks/run-due")
    def run_due():
        as_of = request.args.get("as_of") or (_body() if request.is_json else {}).get("as_of")
        sent: list[dict[str, Any]] = []

        def sink(message):
            sent.append(message)
            if transport is not None:
                transport(message)

        with engine.begin() as conn:
            triggered = svc.run_due_escalations(conn, as_of=as_of)
            dispatched = svc.dispatch_pending_notifications(conn, sink)
        return jsonify(triggered_escalations=triggered, dispatched=len(dispatched),
                       notifications=dispatched)

    # -------------------------------------------------------- 视图
    @api.get("/recalls/<recall_id>/dashboard")
    def dashboard(recall_id: str):
        as_of = request.args.get("as_of")
        with engine.begin() as conn:
            result = svc.hq_dashboard(conn, actor_id=_actor_id(), recall_id=recall_id, as_of=as_of)
        return jsonify(result)

    @api.get("/targets/<target_id>/briefing")
    def briefing(target_id: str):
        with engine.begin() as conn:
            result = svc.store_briefing(conn, actor_id=_actor_id(), target_id=target_id)
        return jsonify(result)

    @api.errorhandler(svc.DomainError)
    def _domain_error(err: svc.DomainError):
        return jsonify(error=type(err).__name__, message=str(err)), err.status

    return api

"""Flask 应用入口：召回触达 HTTP 边界。

鉴权以 X-Actor-Id 表示当前操作者；设备离线上传回执允许匿名（凭 device_id+流水自证）。
每个请求在一个事务内提交，服务层只负责规则。
"""

from flask import Flask, jsonify, request
from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import services as svc
from .database import create_database_engine

FACT_KINDS = {"brand", "operator", "actor", "store", "online_entry"}


def create_app(engine: Engine | None = None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    def _body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise svc.ServiceError(400, "BAD_JSON", "请求体必须是 JSON 对象。")
        return data

    def _actor(conn):
        actor_id = request.headers.get("X-Actor-Id")
        if not actor_id:
            return None
        return svc.require_actor(conn, actor_id)

    def _run(action, *, require_actor: bool = True):
        with storage.begin() as conn:
            # X-Current-Time 用于演练与测试重放；生产由调度器传真实 UTC，缺省取系统时钟。
            current = request.headers.get("X-Current-Time")
            token = svc.set_current_time(current) if current else None
            try:
                actor = _actor(conn)
                if require_actor and actor is None:
                    raise svc.ServiceError(401, "UNAUTHENTICATED", "缺少 X-Actor-Id。")
                return action(conn, actor)
            finally:
                if token is not None:
                    svc.reset_current_time(token)

    @app.errorhandler(svc.ServiceError)
    def _on_error(error: svc.ServiceError):
        return jsonify(error=error.code, message=str(error)), error.status

    # ---------- 身份事实与关系 ----------

    @app.put("/admin/facts/<kind>")
    def put_fact(kind: str):
        if kind not in FACT_KINDS:
            raise svc.ServiceError(400, "UNKNOWN_FACT", f"未知身份事实类型：{kind}")

        def action(conn, actor):
            return svc.put_identity_fact(conn, kind, _body())

        # 种子/主数据接口，部署在管理网络内；业务接口全部强制操作者鉴权。
        return jsonify(_run(action, require_actor=False))

    @app.post("/relations")
    def create_relation():
        payload = _body()

        def action(conn, actor):
            return svc.add_relation(conn, actor, payload)

        return jsonify(_run(action)), 201

    @app.post("/supplies")
    def create_supply():
        payload = _body()

        def action(conn, actor):
            return svc.add_supply(conn, payload)

        # 由溯源/入库系统对接写入，不经门店操作者。
        return jsonify(_run(action, require_actor=False)), 201

    # ---------- 召回与版本纠错 ----------

    @app.post("/recalls")
    def create_recall():
        payload = _body()

        def action(conn, actor):
            return svc.create_recall(conn, actor, payload)

        return jsonify(_run(action)), 201

    @app.post("/recalls/<recall_id>/corrections")
    def correct_batch(recall_id: str):
        payload = _body()

        def action(conn, actor):
            return svc.correct_batch(conn, actor, recall_id, payload)

        return jsonify(_run(action)), 201

    # ---------- 回执（设备离线上传允许匿名） ----------

    @app.post("/receipts")
    def submit_receipt():
        payload = _body()

        def action(conn, actor):
            return svc.submit_receipt(conn, actor, payload)

        return jsonify(_run(action, require_actor=False)), 201

    # ---------- 异议复核与人工改派 ----------

    @app.post("/disputes/<dispute_id>/review")
    def review_dispute(dispute_id: str):
        payload = _body()

        def action(conn, actor):
            return svc.review_dispute(conn, actor, dispute_id, payload)

        return jsonify(_run(action))

    @app.post("/versions/<version_id>/targets/<ttype>/<tid>/reassign")
    def reassign(version_id: str, ttype: str, tid: str):
        payload = _body()

        def action(conn, actor):
            return svc.reassign_target(conn, actor, version_id, ttype, tid, payload)

        return jsonify(_run(action))

    # ---------- 时钟推进（生产由调度器周期调用；测试可注入时刻） ----------

    @app.post("/sweep")
    def sweep():
        at = request.args.get("at")

        def action(conn, actor):
            return svc.run_sweep(conn, at)

        # 系统调度端点，不经人工操作者。
        return jsonify(_run(action, require_actor=False))

    # ---------- 按版本视图（总部：仅所属品牌） ----------

    @app.get("/versions/<version_id>/coverage")
    def coverage(version_id: str):
        def action(conn, actor):
            return svc.coverage_view(conn, actor, version_id)

        return jsonify(_run(action))

    @app.get("/versions/<version_id>/risk-entries")
    def risk_entries(version_id: str):
        def action(conn, actor):
            return svc.risk_entries_view(conn, actor, version_id)

        return jsonify(_run(action))

    @app.get("/versions/<version_id>/targets")
    def targets(version_id: str):
        def action(conn, actor):
            return svc.targets_view(conn, actor, version_id)

        return jsonify(_run(action))

    @app.get("/versions/<version_id>/targets/<ttype>/<tid>/timeline")
    def timeline(version_id: str, ttype: str, tid: str):
        def action(conn, actor):
            return svc.timeline_view(conn, actor, version_id, ttype, tid)

        return jsonify(_run(action))

    # ---------- 门店/经营主体最小信息视图 ----------

    @app.get("/me/tasks")
    def my_tasks():
        def action(conn, actor):
            return svc.my_tasks_view(conn, actor)

        return jsonify(_run(action))

    @app.get("/notifications")
    def notifications():
        def action(conn, actor):
            return svc.pending_notifications_view(conn, actor)

        return jsonify(_run(action))

    return app

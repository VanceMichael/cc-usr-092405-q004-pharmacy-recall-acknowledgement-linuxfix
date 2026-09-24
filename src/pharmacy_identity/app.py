"""Flask 应用入口。"""

from flask import Flask, jsonify
from sqlalchemy import text

from .api import build_api
from .database import create_database_engine


def create_app(engine=None, transport=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()
    app.register_blueprint(build_api(storage, transport), url_prefix="/v1")

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    return app

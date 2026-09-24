"""共享测试夹具：内存 SQLite + 建表。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from pharmacy_identity.schema import metadata


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(eng)
    return eng

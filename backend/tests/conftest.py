import os
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "samples"))

# Tests get their own database and storage, set before the app reads its settings.
_base_url = os.environ.get("DATABASE_URL") or dotenv_values(ROOT / "backend" / ".env").get("DATABASE_URL")
_test_url = None
if _base_url:
    _test_url = _base_url.rsplit("/", 1)[0] + "/redline_test"
    os.environ["DATABASE_URL"] = _test_url
os.environ["STORAGE_DIR"] = tempfile.mkdtemp(prefix="redline-tests-")
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"
os.environ["PINECONE_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""

from build_templates import build_all  # noqa: E402


@pytest.fixture(scope="session")
def templates(tmp_path_factory) -> dict[str, Path]:
    return build_all(tmp_path_factory.mktemp("templates"))


@pytest.fixture(scope="session")
def database():
    if not _test_url:
        pytest.skip("No DATABASE_URL configured")
    plain = _test_url.replace("postgresql+psycopg://", "postgresql://")
    admin = plain.rsplit("/", 1)[0] + "/postgres"
    try:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("DROP DATABASE IF EXISTS redline_test WITH (FORCE)")
            connection.execute("CREATE DATABASE redline_test")
    except psycopg.OperationalError as exc:
        pytest.skip(f"PostgreSQL is not available: {exc}")

    from app import models  # noqa: F401
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    yield
    engine.dispose()

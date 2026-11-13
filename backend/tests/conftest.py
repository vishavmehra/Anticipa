"""Shared fixtures for every module's tests (deliver a working, tested, demonstrable Anticipa MVP).

Auth (no network, no Supabase):
  jwks               patches `PyJWKClient.fetch_data` so tokens signed with a throw-away EC P-256 key verify.
  auth_headers(role) -> {"Authorization": "Bearer <ES256 token>"} with `app_metadata.anticipa_role = role`.
  mint_token(...)    full control over claims (aud/iss/exp/app_metadata/...) for negative tests.

API clients:
  api_client_nodb    TestClient without a database (auth/RBAC/envelope tests).
  api_client         TestClient on DB `anticipa_test`; every request runs in the test's rolled-back
  transaction.

Database (integration; skipped when local Postgres is unreachable):
  test_engine        session-scoped; creates DB `anticipa_test` if missing and migrates it to head. It never
                     touches the dev database `anticipa`, and refuses non-local servers.
  db_session         Session in a transaction that is rolled back after the test (commit() only releases a
  SAVEPOINT).

Tests that use `test_engine`, `db_session` or `api_client` are marked `integration` automatically.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import psycopg
import pytest
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm
from jwt.exceptions import PyJWKClientConnectionError
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from anticipa.config import ConfigError, Settings, get_settings
from anticipa.core import auth as core_auth
from anticipa.db import make_engine
from anticipa.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB_NAME = "anticipa_test"  # the database integration tests may create or modify...
DDL_DB_NAME = "anticipa_test_ddl"  # ...and this one, for lock-heavy DDL tests (RLS/GRANT) that would deadlock
# with other suites sharing anticipa_test. Never the dev database `anticipa`.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
SETUP_LOCK_KEY = 7_283_401  # serialises create+migrate when several pytest processes start at once
SUPABASE_TEST_URL = "https://test-project.supabase.co"
TEST_KID = "test-key-1"
INTEGRATION_FIXTURES = frozenset({"test_engine", "db_session", "api_client", "ddl_engine", "ddl_session"})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_FIXTURES & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.integration)


# --------------------------------------------------------------------------------------------- settings


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Settings that ignore `.env`; no real project, key or database is referenced."""
    return Settings(_env_file=None, env="local", supabase_url=SUPABASE_TEST_URL, profile_touch_seconds=0)


# --------------------------------------------------------------------------------------------- JWT / JWKS


class JwksStub:
    """Controls the (patched) JWKS endpoint: the active signing key, and an 'unreachable' switch."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, kid: str) -> None:
        self.private_key = private_key
        self.kid = kid
        self.unreachable = False
        self._jwk = {
            **ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True),
            "kid": kid,
            "use": "sig",
            "alg": "ES256",
        }

    def fetch(self) -> dict[str, Any]:
        if self.unreachable:
            raise PyJWKClientConnectionError("jwks unreachable (test)")
        return {"keys": [self._jwk]}


@pytest.fixture(scope="session")
def _signing_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def jwks(monkeypatch: pytest.MonkeyPatch, _signing_key: ec.EllipticCurvePrivateKey) -> Iterator[JwksStub]:
    stub = JwksStub(_signing_key, TEST_KID)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: stub.fetch())
    core_auth.get_jwks_client.cache_clear()
    yield stub
    core_auth.get_jwks_client.cache_clear()


def role_user_id(role: str) -> uuid.UUID:
    """Stable per-role user id used by `auth_headers`."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{role}.anticipa.test")


@pytest.fixture
def mint_token(jwks: JwksStub) -> Callable[..., str]:
    def mint(
        role: str | None = "observer",
        *,
        sub: str | uuid.UUID | None = None,
        email: str | None = None,
        aud: str | None = "authenticated",
        iss: str | None = f"{SUPABASE_TEST_URL}/auth/v1",
        expires_in: float | None = 3600,
        app_metadata: dict[str, Any] | None = None,
        user_metadata: dict[str, Any] | None = None,
        key: ec.EllipticCurvePrivateKey | str | bytes | None = None,
        algorithm: str = "ES256",
        kid: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "sub": str(sub or role_user_id(role or "norole")),
            "email": email or f"{role or 'norole'}@example.test",
            "iat": now,
            "app_metadata": app_metadata
            if app_metadata is not None
            else ({"anticipa_role": role} if role else {}),
            "user_metadata": user_metadata or {},
        }
        if aud is not None:
            claims["aud"] = aud
        if iss is not None:
            claims["iss"] = iss
        if expires_in is not None:
            claims["exp"] = now + timedelta(seconds=expires_in)
        return jwt.encode(
            claims,
            key if key is not None else jwks.private_key,
            algorithm=algorithm,
            headers={"kid": kid or jwks.kid},
        )

    return mint


@pytest.fixture
def auth_headers(mint_token: Callable[..., str]) -> Callable[[str], dict[str, str]]:
    def headers(role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {mint_token(role)}"}

    return headers


# --------------------------------------------------------------------------------------------- API clients


@pytest.fixture
def api_client_nodb(
    test_settings: Settings, jwks: JwksStub, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """App wired to an engine that is never connected; the profile upsert is stubbed out."""
    monkeypatch.setattr(core_auth, "touch_profile", lambda *args, **kwargs: None)
    engine = create_engine(
        "postgresql+psycopg://nobody:nobody@127.0.0.1:1/none"
    )  # connection refused if ever used
    with TestClient(create_app(test_settings, engine=engine), raise_server_exceptions=False) as client:
        yield client
    engine.dispose()


# --------------------------------------------------------------------------------------------- database


def _local_database_url() -> URL:
    try:
        url = make_url(get_settings().sqlalchemy_url())
    except ConfigError as exc:
        pytest.skip(f"integration tests need a local database: {exc}")
    if url.host not in LOCAL_HOSTS and not (url.host or "").startswith("127."):
        pytest.skip("refusing to create a test database on a non-local server")
    return url.set(host="127.0.0.1" if url.host == "localhost" else url.host)


@pytest.fixture(scope="session")
def test_database_url() -> URL:
    return _local_database_url().set(database=TEST_DB_NAME)


def _ensure_test_database(url: URL) -> None:
    """Create the test database if missing and migrate it to head, under a cluster-wide advisory lock."""
    assert url.database in {TEST_DB_NAME, DDL_DB_NAME}  # never operate on any other database
    admin = url.set(database="postgres")
    conninfo = psycopg.conninfo.make_conninfo(
        host=admin.host,
        port=admin.port,
        user=admin.username,
        password=admin.password,
        dbname="postgres",
        connect_timeout=5,
    )
    try:
        conn = psycopg.connect(conninfo, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"Postgres is not reachable ({type(exc).__name__}); start it with `docker compose up -d postgres`"
        )
    with conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (SETUP_LOCK_KEY,))
        try:
            exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,)).fetchone()
            if not exists:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
            cfg = Config(str(BACKEND_DIR / "alembic.ini"))
            cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
            cfg.attributes["url"] = url
            cfg.attributes["configure_logging"] = False
            command.upgrade(cfg, "head")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (SETUP_LOCK_KEY,))


@pytest.fixture(scope="session")
def test_engine(test_database_url: URL) -> Iterator[Engine]:
    _ensure_test_database(test_database_url)
    engine = make_engine(test_database_url, pool_size=5, max_overflow=5)
    yield engine
    engine.dispose()


def _rolled_back_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def db_session(test_engine: Engine) -> Iterator[Session]:
    """Transactional session: whatever the test (or the code under test) commits is rolled back afterwards."""
    yield from _rolled_back_session(test_engine)


@pytest.fixture(scope="session")
def ddl_engine(test_database_url: URL) -> Iterator[Engine]:
    """Separate migrated database for tests that take ACCESS EXCLUSIVE locks (ALTER TABLE ... RLS, GRANT)."""
    url = test_database_url.set(database=DDL_DB_NAME)
    _ensure_test_database(url)
    engine = make_engine(url, pool_size=2, max_overflow=2)
    yield engine
    engine.dispose()


@pytest.fixture
def ddl_session(ddl_engine: Engine) -> Iterator[Session]:
    """Like `db_session`, on the DDL database (DDL is transactional in PostgreSQL, so it rolls back too)."""
    yield from _rolled_back_session(ddl_engine)


@pytest.fixture
def api_client(
    test_settings: Settings, test_engine: Engine, db_session: Session, jwks: JwksStub
) -> Iterator[TestClient]:
    """TestClient whose request sessions share the test's connection (so nothing outlives the test)."""
    app = create_app(test_settings, engine=test_engine)
    app.state.sessionmaker = sessionmaker(
        bind=db_session.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

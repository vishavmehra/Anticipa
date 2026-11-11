# ruff: noqa: E501  (long docstring/comment lines)
"""Fixtures for the evidence tests. Unit tests need nothing; `integration` tests use the local docker Postgres, and
ONLY the database `anticipa_test` (created and migrated on demand; the dev database `anticipa` is never touched)."""

from __future__ import annotations

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).parent))  # lets tests `import evidence_samples` (importlib import mode)

BACKEND = Path(__file__).resolve().parents[2]
TEST_DB = "anticipa_test"
MIGRATION_LOCK = 72_727_201  # pg advisory lock: other workstreams' fixtures may migrate the same database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def evidence_db():
    """Session factory bound to the migrated `anticipa_test` database (skips when Postgres is unreachable)."""
    from alembic.config import Config
    from sqlalchemy.engine import URL
    from sqlalchemy.exc import OperationalError

    from alembic import command
    from anticipa.config import Settings
    from anticipa.db import make_engine, make_sessionmaker

    settings = Settings()
    if not settings.is_local or settings.database_url is not None:
        pytest.skip("integration tests run only against the local docker Postgres")
    # Not settings.sqlalchemy_url(): that needs a configured password. The local container's documented dev default
    # (.env.example / docker-compose.yml) is the fallback, and the database name is always forced to anticipa_test.
    password = (
        settings.postgres_password.get_secret_value() if settings.postgres_password else "anticipa-dev-only"
    )
    host = "127.0.0.1" if settings.postgres_host == "localhost" else settings.postgres_host
    # The compose port may be remapped (POSTGRES_PORT) when another Postgres owns 5432: try the configured one first.
    for port in dict.fromkeys([settings.postgres_port, 5433]):
        base = URL.create(
            "postgresql+psycopg",
            username=settings.postgres_user,
            password=password,
            host=host,
            port=port,
            database="postgres",
            query={"connect_timeout": "3"},
        )
        try:
            admin = make_engine(base, isolation_level="AUTOCOMMIT")
            with admin.connect() as c:
                if not c.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB}
                ).scalar():
                    c.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
            admin.dispose()
            break
        except OperationalError:
            continue
    else:
        pytest.skip("local Postgres is not reachable (docker compose up -d postgres)")
    url = base.set(database=TEST_DB)
    engine = make_engine(url, pool_size=10, max_overflow=20)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK})
        try:
            c.execute(text("CREATE SCHEMA IF NOT EXISTS extensions"))
            c.execute(text("CREATE EXTENSION IF NOT EXISTS vector SCHEMA extensions"))
            cfg = Config()
            cfg.set_main_option("script_location", str(BACKEND / "alembic"))
            cfg.attributes["url"] = url
            cfg.attributes["configure_logging"] = False
            command.upgrade(cfg, "head")
        finally:
            c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK})
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def sessions(evidence_db) -> Iterator[sessionmaker[Session]]:
    yield evidence_db


class World:
    """Rows created by one test (unique ids), deleted afterwards by cascade from the dataset's residents."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        from anticipa.datafabric.models import Dataset

        self.sessions = sessions
        self.tag = uuid.uuid4().hex[:10]
        self.dataset_id = uuid.uuid4()
        self.residents: list[uuid.UUID] = []
        self.documents: list[uuid.UUID] = []
        self.jobs: list[
            uuid.UUID
        ] = []  # register submitted jobs so cleanup can remove them and their invocations
        with sessions() as s:
            s.add(Dataset(id=self.dataset_id, dataset_version=f"test-{self.tag}", generator="pytest"))
            s.commit()

    def resident(self) -> uuid.UUID:
        from anticipa.datafabric.models import Resident

        rid = uuid.uuid4()
        with self.sessions() as s:
            s.add(
                Resident(
                    id=rid,
                    dataset_id=self.dataset_id,
                    source_system="pytest",
                    source_patient_id=f"{self.tag}-{len(self.residents)}",
                )
            )
            s.commit()
        self.residents.append(rid)
        return rid

    def document(
        self, resident_id: uuid.UUID, text_: str, *, document_type="clinical_note", language="en"
    ) -> uuid.UUID:
        import hashlib

        from anticipa.datafabric.models import Document

        did = uuid.uuid4()
        with self.sessions() as s:
            s.add(
                Document(
                    id=did,
                    resident_id=resident_id,
                    document_type=document_type,
                    language=language,
                    storage_key=f"{self.tag}/{did}.txt",
                    content_hash=hashlib.sha256(text_.encode("utf-8")).hexdigest(),
                )
            )
            s.commit()
        self.documents.append(did)
        return did

    def cleanup(self) -> None:
        from sqlalchemy import delete

        from anticipa.ai.models import ModelInvocation
        from anticipa.datafabric.models import Dataset, Resident
        from anticipa.evidence.models import EvidenceFact
        from anticipa.jobs.models import ProcessingJob

        with self.sessions() as s:
            # facts first: evidence_sources.document_id has no ON DELETE rule, so the resident cascade could
            # otherwise try to delete a document that a source still references
            s.execute(delete(EvidenceFact).where(EvidenceFact.resident_id.in_(self.residents)))
            s.execute(delete(Resident).where(Resident.dataset_id == self.dataset_id))
            s.execute(delete(Dataset).where(Dataset.id == self.dataset_id))
            if self.jobs:
                s.execute(delete(ModelInvocation).where(ModelInvocation.job_id.in_(self.jobs)))
                s.execute(delete(ProcessingJob).where(ProcessingJob.id.in_(self.jobs)))
            s.commit()


@pytest.fixture
def world(sessions) -> Iterator[World]:
    w = World(sessions)
    try:
        yield w
    finally:
        w.cleanup()

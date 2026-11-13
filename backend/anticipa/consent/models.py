"""Consent records (core data object, ADR-070): a resident's permit/deny decision for outreach and for AI
processing of their documents. Append-only ledger: a decision is never edited; a change of mind is a newer
record, so history and provenance are kept. What is in force is derived (`consent.service.evaluate`): per
(scope, channel) the newest record that is currently effective wins, and any effective `deny` blocks (deny
wins across the blanket and per-channel levels). No record at all means permit for own-record outreach (the
synthetic default).

FHIR R4 Consent mapping (`service.to_fhir`): `resident_id` -> Consent.patient, `permit|deny` ->
provision.type, `effective_from/to` -> provision.period, `scope` -> provision.purpose, `channel` -> an
extension, "in force" -> Consent.status (active | inactive), `created_at` -> dateTime. `recorded_by` is
staff, not the consenting party, so it is not a Consent.performer. Shaped as FHIR; not validated against a
FHIR server.

Every row is synthetic or simulated (CHECK): `synthetic_seed` rows are `synthetic`, rows entered through the
API are `simulated`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from anticipa.datafabric import models as _datafabric_models  # noqa: F401  (residents for the FK)
from anticipa.db import UUID_PK, Base, sql_in
from anticipa.engagement.models import CHANNELS, LANGUAGES

SCOPES = ("outreach", "data_use_ai_processing")
OUTREACH, AI_PROCESSING = SCOPES
STATUSES = ("permit", "deny")
SOURCES = ("resident_request", "coordinator_entry", "synthetic_seed")
SYNTHETIC_STATUSES = ("synthetic", "simulated")


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    __table_args__ = (
        CheckConstraint(sql_in("scope", SCOPES), name="scope"),
        CheckConstraint(sql_in("status", STATUSES), name="status"),
        CheckConstraint(sql_in("channel", CHANNELS), name="channel"),  # NULL passes: blanket (all channels)
        CheckConstraint(sql_in("language", LANGUAGES), name="language"),
        CheckConstraint(sql_in("source", SOURCES), name="source"),
        CheckConstraint(sql_in("synthetic_status", SYNTHETIC_STATUSES), name="synthetic_status"),
        CheckConstraint("scope = 'outreach' OR channel IS NULL", name="channel_only_for_outreach"),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="period_order"),
        Index("ix_consent_records_resident", "resident_id", "scope", "channel", "effective_from"),
        Index("ix_consent_records_deny", "resident_id", postgresql_where=text("status = 'deny'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, **UUID_PK)
    resident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("residents.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str | None] = mapped_column(Text)  # outreach only; NULL = every channel
    language: Mapped[str | None] = mapped_column(Text)  # language the decision was given in
    source: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )  # staff user (JWT sub); NULL = system/seed
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )  # actor_role, request_id, short note (no identifiers), seed
    synthetic_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="simulated", default="simulated"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

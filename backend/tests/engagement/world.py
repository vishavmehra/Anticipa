"""Test-world builder shared by the engagement and closure tests (loaded by path from both conftests).

Builds residents, care-gap cases (any status), a simulated clinic with slots, a completed planning run and planned
allocations directly in the test transaction. Everything is clearly synthetic/simulated (deliver a working, tested,
demonstrable Anticipa MVP); the cases are inserted with the status the scenario needs, the code under test then drives
them through the real lifecycle.

Deliberately independent of the guideline engine: a case row needs evaluation / guideline-version / rule-version ids, but
these tests are about engagement and closure, so foreign-key triggers are switched off for the whole (rolled-back) test
transaction (`session_replication_role = replica`, local docker superuser only; CHECK and UNIQUE constraints still apply)
and those three ids are plain random UUIDs. Postgres re-checks a row's FKs on UPDATE when the row was inserted in the same
transaction, which is why the switch cannot be scoped to the insert alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from anticipa.caregaps.models import CareGapCase
from anticipa.core.audit import Actor
from anticipa.datafabric.models import ClinicalRecord, Dataset, Resident
from anticipa.planning import service as planning
from anticipa.planning.models import CapacitySlot, Clinic, ClinicService, PlannedAllocation, PlanningRun

T0 = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
ACTIVITY = "colorectal_cancer_screening"
ADMIN = Actor(uuid.uuid5(uuid.NAMESPACE_DNS, "population_admin.anticipa.test"), "population_admin")
SYSTEM = Actor(ADMIN.id, "system")


class World:
    def __init__(self, session: Session) -> None:
        self.s = session
        self.n = 0
        session.execute(text("SET LOCAL session_replication_role = replica"))  # FK triggers off for this (rolled-back) test transaction
        self.dataset = Dataset(dataset_version=f"syn-test-{uuid.uuid4().hex[:8]}", generator="test")
        self.clinic = Clinic(clinic_key=f"test-{uuid.uuid4().hex[:8]}", name="Test clinic (simulated)", district_code="D1")
        session.add_all([self.dataset, self.clinic])
        session.flush()
        self.service = ClinicService(clinic_id=self.clinic.id, service_key=ACTIVITY, service_name="Screening",
                                     appointment_duration_minutes=30)  # fmt: skip
        self.run = PlanningRun(planning_period_start=T0, planning_period_end=T0 + timedelta(days=30), configuration_version="t",
                               algorithm_version="t", seed=1, status="completed", completed_at=datetime.now(UTC))  # fmt: skip
        session.add_all([self.service, self.run])
        session.flush()

    def sessions(self) -> sessionmaker[Session]:
        """New sessions that join the test's rolled-back transaction (what the API and job code open per unit of work)."""
        return sessionmaker(bind=self.s.get_bind(), join_transaction_mode="create_savepoint", expire_on_commit=False)

    def resident(self, language: str = "en") -> Resident:
        r = Resident(
            dataset_id=self.dataset.id, source_system="test", source_patient_id=uuid.uuid4().hex, birth_date=date(1970, 1, 1),
            administrative_sex="female", preferred_language=language, district_code="D1",
        )  # fmt: skip
        self.s.add(r)
        self.s.flush()
        return r

    def record(self, resident_id: uuid.UUID, category: str = "procedure") -> ClinicalRecord:
        rec = ClinicalRecord(
            resident_id=resident_id, resource_type=category.capitalize(), source_resource_id=uuid.uuid4().hex, category=category,
            code_system="urn:test:synthetic", code="PROC-1", effective_start=datetime(2026, 11, 1, tzinfo=UTC), status="completed",
            source_reference=f"test://{category}/PROC-1",
        )  # fmt: skip
        self.s.add(rec)
        self.s.flush()
        return rec

    def case(self, status: str = "planned", language: str = "en", activity: str = ACTIVITY, **kw) -> CareGapCase:
        r = self.resident(language)
        case = CareGapCase(
            resident_id=r.id, evaluation_id=uuid.uuid4(), guideline_version_id=uuid.uuid4(), rule_version_id=uuid.uuid4(),
            care_activity=activity, status=status, priority="routine", source_outcome="eligible", synthetic_status="synthetic", **kw,
        )  # fmt: skip
        self.s.add(case)
        self.s.flush()
        return case

    def allocation(self, case: CareGapCase, *, confirm: bool = True, seats: int = 2, past: bool = False) -> PlannedAllocation:
        self.n += 1
        start = (datetime.now(UTC) - timedelta(days=2) if past else T0) + timedelta(minutes=30 * self.n)
        slot = CapacitySlot(clinic_id=self.clinic.id, clinic_service_id=self.service.id, start_time=start,
                            end_time=start + timedelta(minutes=30), available_capacity=seats, source_type="manual")  # fmt: skip
        self.s.add(slot)
        self.s.flush()
        alloc = PlannedAllocation(
            planning_run_id=self.run.id, care_gap_case_id=case.id, resident_id=case.resident_id, clinic_id=self.clinic.id,
            clinic_service_id=self.service.id, capacity_slot_id=slot.id, clinic_key=self.clinic.clinic_key,
            service_key=ACTIVITY, proposed_start_time=start, proposed_end_time=start + timedelta(minutes=30),
        )  # fmt: skip
        self.s.add(alloc)
        self.s.flush()
        if confirm:
            planning.confirm_allocation(self.s, alloc.id, ADMIN)
        return alloc

    def planned(self, language: str = "en", *, confirm: bool = True, status: str = "planned") -> tuple[CareGapCase, PlannedAllocation]:
        case = self.case(status, language)
        return case, self.allocation(case, confirm=confirm)

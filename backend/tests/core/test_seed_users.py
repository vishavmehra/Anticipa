"""`anticipa seed-users`: Supabase Admin API calls are mocked with respx (Supabase is never contacted)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from anticipa import cli
from anticipa.config import Settings
from anticipa.core.supabase_admin import DEMO_USERS, SeedError, record_seed_audit, seed_users

BASE = "https://test-project.supabase.co"
USERS_URL = f"{BASE}/auth/v1/admin/users"
SECRET = "sb_secret_unit-test-key"  # noqa: S105
PASSWORD = "unit-test-password-123"  # noqa: S105


def settings(**kw) -> Settings:
    kw.setdefault("supabase_secret_key", SecretStr(SECRET))
    return Settings(_env_file=None, env="local", supabase_url=BASE, **kw)


def test_the_five_demo_users_map_to_their_roles():
    assert {(e, r.value) for e, r, _ in DEMO_USERS} == {
        ("anticipa.judge@example.com", "observer"),
        ("anticipa.admin@example.com", "population_admin"),
        ("anticipa.coordinator@example.com", "care_coordinator"),
        ("anticipa.reviewer@example.com", "clinical_reviewer"),
        ("anticipa.sysadmin@example.com", "system_admin"),
    }


@respx.mock
def test_creates_missing_users_with_confirmed_email_and_role_in_app_metadata():
    listing = respx.get(USERS_URL).mock(return_value=httpx.Response(200, json={"users": []}))
    ids = iter(f"00000000-0000-4000-8000-00000000000{i}" for i in range(1, 6))
    create = respx.post(USERS_URL).mock(side_effect=lambda req: httpx.Response(200, json={"id": next(ids)}))

    results = seed_users(settings(), PASSWORD)

    assert listing.call_count == 1 and create.call_count == 5
    assert [r.action for r in results] == ["created"] * 5
    for call, (email, role, _) in zip(create.calls, DEMO_USERS, strict=True):
        body = json.loads(call.request.content)
        assert body["email"] == email and body["password"] == PASSWORD and body["email_confirm"] is True
        assert body["app_metadata"] == {"anticipa_role": role.value}
        assert "anticipa_role" not in body["user_metadata"]
        assert call.request.headers["apikey"] == SECRET
        assert call.request.headers["authorization"] == f"Bearer {SECRET}"


@respx.mock
def test_is_idempotent_updating_existing_users_by_email_case_insensitively():
    existing = [{"id": "11111111-1111-4111-8111-111111111111", "email": "Anticipa.Judge@example.com"}]
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json={"users": existing}))
    update = respx.put(f"{USERS_URL}/11111111-1111-4111-8111-111111111111").mock(
        return_value=httpx.Response(200, json={"id": "11111111-1111-4111-8111-111111111111"})
    )
    create = respx.post(USERS_URL).mock(
        return_value=httpx.Response(200, json={"id": "22222222-2222-4222-8222-222222222222"})
    )

    results = seed_users(settings(), PASSWORD)

    assert [r.action for r in results] == ["updated", "created", "created", "created", "created"]
    assert update.call_count == 1 and create.call_count == 4
    body = json.loads(update.calls[0].request.content)
    assert body["email_confirm"] is True and body["app_metadata"] == {"anticipa_role": "observer"}


@respx.mock
def test_listing_follows_pages():
    page1 = [{"id": f"00000000-0000-4000-8000-{i:012d}", "email": f"u{i}@x.test"} for i in range(200)]
    page2 = [{"id": "11111111-1111-4111-8111-111111111111", "email": "anticipa.admin@example.com"}]
    respx.get(USERS_URL, params={"page": 1, "per_page": 200}).mock(
        return_value=httpx.Response(200, json={"users": page1})
    )
    respx.get(USERS_URL, params={"page": 2, "per_page": 200}).mock(
        return_value=httpx.Response(200, json={"users": page2})
    )
    respx.put(f"{USERS_URL}/11111111-1111-4111-8111-111111111111").mock(
        return_value=httpx.Response(200, json={"id": "x"})
    )
    respx.post(USERS_URL).mock(
        return_value=httpx.Response(200, json={"id": "22222222-2222-4222-8222-222222222222"})
    )
    assert [r.action for r in seed_users(settings(), PASSWORD)].count("updated") == 1


@respx.mock
def test_empty_secret_key_fails_clearly_without_any_request():
    route = respx.route().mock(return_value=httpx.Response(500))
    for key in (None, SecretStr(""), SecretStr("   "), SecretStr("sb_secret_replace-me")):
        with pytest.raises(SeedError, match="SUPABASE_SECRET_KEY is empty"):
            seed_users(settings(supabase_secret_key=key), PASSWORD)
    assert route.call_count == 0


@pytest.mark.parametrize("password", [None, "", "short"])
@respx.mock
def test_missing_or_short_password_fails_without_any_request(password):
    route = respx.route().mock(return_value=httpx.Response(500))
    with pytest.raises(SeedError, match="ANTICIPA_DEMO_PASSWORD"):
        seed_users(settings(), password)
    assert route.call_count == 0


def test_missing_supabase_url_fails():
    with pytest.raises(SeedError, match="SUPABASE_URL"):
        seed_users(
            Settings(_env_file=None, supabase_url=None, supabase_secret_key=SecretStr(SECRET)), PASSWORD
        )


@respx.mock
def test_supabase_errors_are_reported_without_leaking_secrets():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json={"users": []}))
    respx.post(USERS_URL).mock(
        return_value=httpx.Response(422, json={"msg": "Password should be at least 6 characters"})
    )
    with pytest.raises(SeedError) as err:
        seed_users(settings(), PASSWORD)
    assert "HTTP 422" in str(err.value) and "anticipa.judge@example.com" in str(err.value)
    assert SECRET not in str(err.value) and PASSWORD not in str(err.value)


@respx.mock
def test_unauthorized_and_unreachable_are_reported():
    respx.get(USERS_URL).mock(return_value=httpx.Response(401, json={"message": "Invalid API key"}))
    with pytest.raises(SeedError, match="HTTP 401"):
        seed_users(settings(), PASSWORD)
    respx.get(USERS_URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(SeedError, match="unreachable"):
        seed_users(settings(), PASSWORD)


@respx.mock
def test_cli_prints_a_summary_and_never_the_password_or_key(monkeypatch, capsys):
    s = settings(demo_password=SecretStr(PASSWORD))
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    monkeypatch.setattr(
        "anticipa.db.get_sessionmaker", lambda: (_ for _ in ()).throw(RuntimeError("no db in unit test"))
    )
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json={"users": []}))
    respx.post(USERS_URL).mock(
        return_value=httpx.Response(200, json={"id": "22222222-2222-4222-8222-222222222222"})
    )
    assert cli.main(["seed-users"]) == 0
    out = capsys.readouterr()
    assert "anticipa.judge@example.com" in out.out and "observer" in out.out
    assert PASSWORD not in out.out + out.err and SECRET not in out.out + out.err
    assert "audit events could not be stored" in out.err  # DB unavailable here: provisioning still succeeded


def test_cli_without_secret_key_exits_2_with_instructions(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: settings(supabase_secret_key=SecretStr(""), demo_password=SecretStr(PASSWORD)),
    )
    assert cli.main(["seed-users"]) == 2
    assert "SUPABASE_SECRET_KEY is empty" in capsys.readouterr().err


def test_record_seed_audit_stages_one_event_per_user(db_session):
    from sqlalchemy import select

    from anticipa.core.models import AuditEvent
    from anticipa.core.supabase_admin import SeedResult

    record_seed_audit(
        db_session,
        [
            SeedResult("a@x.test", "observer", "created", "u1"),
            SeedResult("b@x.test", "system_admin", "updated", "u2"),
        ],
    )
    rows = (
        db_session.execute(select(AuditEvent).where(AuditEvent.resource_id.in_(["u1", "u2"]))).scalars().all()
    )
    assert {(r.action, r.details["role"]) for r in rows} == {
        ("auth.user_created", "observer"), ("auth.user_updated", "system_admin")
    }  # fmt: skip
    assert all("@" not in str(r.details) for r in rows)  # no email addresses in audit details


def test_module_clis_are_mounted_under_anticipa(capsys):
    assert "planning" in cli.module_clis()
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["planning", "--help"])  # delegated to anticipa.planning.cli, which owns its sub-commands
    assert exit_info.value.code == 0
    assert "generate-capacity" in capsys.readouterr().out


@respx.mock
def test_judge_login_gets_its_own_password_so_it_cannot_open_a_privileged_account():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json={"users": []}))
    uid = {"id": "00000000-0000-4000-8000-000000000001"}
    create = respx.post(USERS_URL).mock(return_value=httpx.Response(200, json=uid))

    seed_users(settings(), PASSWORD, judge_password="a-different-judge-password")  # noqa: S106

    bodies = [json.loads(c.request.content) for c in create.calls]
    by_email = {b["email"]: b["password"] for b in bodies}
    assert by_email["anticipa.judge@example.com"] == "a-different-judge-password"
    assert {pw for email, pw in by_email.items() if email != "anticipa.judge@example.com"} == {PASSWORD}


def test_a_short_judge_password_is_refused():
    with pytest.raises(SeedError, match="ANTICIPA_JUDGE_PASSWORD"):
        seed_users(settings(), PASSWORD, judge_password="short")  # noqa: S106

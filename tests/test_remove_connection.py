"""scripts/remove_connection.py: getting rid of a stored database URL,
including one that no longer decrypts, the case it exists for."""

import datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import ApiKey, InviteCode, Workspace
from app.auth.service import AuthenticationError, authenticate
from app.auth.signup import create_invite
from app.auth.workspaces import WorkspaceSpec, create_workspace
from app.db import SessionLocal
from app.query import schema_cache
from app.query.models import DataSourceConnection, QueryExecution, SchemaCache
from app.query.service import run_query
from app.query.target import TARGET_UNREACHABLE
from scripts.remove_connection import clean_up

pytestmark = pytest.mark.asyncio

WORKSPACE_NAME = "test-workspace"
# A URL encrypted under a key the server no longer has.
STALE_URL = "postgresql://someone:old-password@old-host.example:5432/old"
STALE_CIPHERTEXT = Fernet(Fernet.generate_key()).encrypt(STALE_URL.encode()).decode()


async def _make_url_stale(session: AsyncSession) -> None:
    data_source = (await session.execute(select(DataSourceConnection))).scalar_one()
    data_source.encrypted_connection_url = STALE_CIPHERTEXT
    await session.commit()


async def _stored_url(session: AsyncSession) -> str | None:
    result = await session.execute(select(DataSourceConnection.encrypted_connection_url))
    return result.scalar_one()


async def test_a_url_that_no_longer_decrypts_cant_be_used_and_the_attempt_is_audited(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await _make_url_stale(db_session)

    response = await run_query(db_session, workspace, "select 1")

    assert response["status"] == "errored"
    assert response["error"] == TARGET_UNREACHABLE
    audit_row = (await db_session.execute(select(QueryExecution))).scalar_one()
    assert audit_row.rejection_reason == TARGET_UNREACHABLE


async def test_without_apply_nothing_changes(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await _make_url_stale(db_session)

    lines = await clean_up(WORKSPACE_NAME, should_remove_workspace=True, apply=False)

    assert "Nothing changed" in lines[-1]
    assert await _stored_url(db_session) == STALE_CIPHERTEXT


async def test_a_stale_url_is_cleared_without_being_read(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await _make_url_stale(db_session)

    await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=True)

    assert await _stored_url(db_session) is None


async def test_a_cleared_workspace_queries_the_default_database_and_keeps_its_key(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, api_key = seeded_workspace
    await _make_url_stale(db_session)
    await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=True)

    async with SessionLocal() as fresh_session:
        response = await run_query(fresh_session, workspace, "select 1 as answer")
        authenticated = await authenticate(fresh_session, api_key)

    assert response["rows"] == [{"answer": 1}]
    assert authenticated.id == workspace.id


async def test_clearing_drops_the_old_databases_schema_cache(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await schema_cache.describe_table(db_session, workspace, "workspaces")

    await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=True)

    db_session.expire_all()
    assert (await db_session.execute(select(SchemaCache))).scalar_one_or_none() is None


async def test_clearing_a_workspace_with_no_stored_url_does_nothing(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=True)

    lines = await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=True)

    assert lines[-1].startswith("Nothing to do")


async def test_removing_a_workspace_deletes_its_rows_and_its_key_stops_working(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, api_key = seeded_workspace
    await _make_url_stale(db_session)
    await run_query(db_session, workspace, "select 1")

    await clean_up(WORKSPACE_NAME, should_remove_workspace=True, apply=True)

    db_session.expire_all()
    assert (await db_session.execute(select(Workspace))).scalars().all() == []
    assert (await db_session.execute(select(QueryExecution))).scalars().all() == []
    with pytest.raises(AuthenticationError):
        await authenticate(db_session, api_key)


async def test_removing_a_workspace_leaves_other_workspaces_alone(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    other_spec = WorkspaceSpec(
        name="other",
        console_username="other",
        console_password="x" * 12,
        created_via="operator",
    )
    _, other_key = await create_workspace(
        db_session, other_spec, datetime.datetime.now(datetime.UTC)
    )
    await db_session.commit()

    await clean_up(WORKSPACE_NAME, should_remove_workspace=True, apply=True)

    db_session.expire_all()
    assert (await authenticate(db_session, other_key)).name == "other"


async def test_an_invite_a_removed_workspace_used_stays_spent(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    workspace, _ = seeded_workspace
    await create_invite(db_session, "for the test")
    invite = (await db_session.execute(select(InviteCode))).scalar_one()
    invite.used_at = datetime.datetime.now(datetime.UTC)
    invite.used_by_workspace_id = workspace.id
    await db_session.commit()

    await clean_up(WORKSPACE_NAME, should_remove_workspace=True, apply=True)

    db_session.expire_all()
    spent_invite = (await db_session.execute(select(InviteCode))).scalar_one()
    assert spent_invite.used_at is not None
    assert spent_invite.used_by_workspace_id is None


async def test_the_output_never_shows_the_url(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    await _make_url_stale(db_session)

    lines = await clean_up(WORKSPACE_NAME, should_remove_workspace=False, apply=False)

    output = "\n".join(lines)
    assert STALE_CIPHERTEXT not in output
    assert "old-host" not in output


async def test_an_unknown_workspace_stops_with_a_clear_message(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    with pytest.raises(SystemExit, match="found 0"):
        await clean_up("no-such-workspace", should_remove_workspace=True, apply=True)


async def test_two_workspaces_with_one_name_stop_before_anything_is_deleted(
    db_session: AsyncSession, seeded_workspace: tuple[Workspace, str]
) -> None:
    twin = WorkspaceSpec(
        name=WORKSPACE_NAME,
        console_username="twin",
        console_password="x" * 12,
        created_via="operator",
    )
    await create_workspace(db_session, twin, datetime.datetime.now(datetime.UTC))
    await db_session.commit()

    with pytest.raises(SystemExit, match="found 2"):
        await clean_up(WORKSPACE_NAME, should_remove_workspace=True, apply=True)

    assert len((await db_session.execute(select(ApiKey))).scalars().all()) == 2

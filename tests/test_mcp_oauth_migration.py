"""migrations/versions/b8e2c4f1a9d7_mcp_connector_oauth.py against a throwaway
SQLite file: it must apply on the pre-migration schema, enforce the per-user
binding the design relies on, downgrade cleanly, and produce the same schema
the ORM's create_all (which the test suite and a fresh install's fallback use)
would produce."""

import importlib.util

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from claw.db.models import Base, McpConnector

_MIGRATION = "migrations/versions/b8e2c4f1a9d7_mcp_connector_oauth.py"
_NEW_TABLES = {"mcp_connector_auth", "mcp_oauth_clients"}


def _load_migration():
    spec = importlib.util.spec_from_file_location("mcp_oauth_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_schema(engine) -> None:
    """users + mcp_connectors exactly as they were before this migration."""
    legacy = sa.MetaData()
    sa.Table("users", legacy, sa.Column("id", sa.String(32), primary_key=True), sa.Column("email", sa.String))
    connectors = McpConnector.__table__.to_metadata(legacy)
    for constraint in list(connectors.constraints):
        if constraint.name == "uq_connectors_id_owner":
            connectors.constraints.remove(constraint)
    legacy.create_all(engine)


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path}/migrate.db")
    _legacy_schema(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO users (id, email) VALUES ('alice', 'a@x.io'), ('bob', 'b@x.io')"))
        conn.execute(
            sa.text(
                "INSERT INTO mcp_connectors (id, owner_id, name, kind, transport, command, url, env, enabled, "
                "created_at, updated_at) VALUES "
                "('own', 'alice', 'own', 'mcp', 'http', '', 'https://mcp.example.com', '{}', 1, "
                "'2026-01-01', '2026-01-01'), "
                "('global', NULL, 'global', 'mcp', 'http', '', 'https://mcp.example.com', '{}', 1, "
                "'2026-01-01', '2026-01-01')"
            )
        )
    yield engine
    engine.dispose()


def _upgrade(conn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        _load_migration().upgrade()


def _downgrade(conn) -> None:
    with Operations.context(MigrationContext.configure(conn)):
        _load_migration().downgrade()


_AUTH_INSERT = (
    "INSERT INTO mcp_connector_auth (connector_id, owner_id, version, created_at, updated_at) "
    "VALUES (:connector_id, :owner_id, 0, '2026-01-01', '2026-01-01')"
)


def test_upgrade_binds_auth_rows_to_the_owning_user_and_downgrade_restores(engine):
    with engine.begin() as conn:
        _upgrade(conn)
        inspector = sa.inspect(conn)
        assert _NEW_TABLES <= set(inspector.get_table_names())
        assert any(
            u["name"] == "uq_connectors_id_owner" for u in inspector.get_unique_constraints("mcp_connectors")
        )

    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text(_AUTH_INSERT), {"connector_id": "own", "owner_id": "alice"})
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(_AUTH_INSERT), {"connector_id": "global", "owner_id": "alice"})
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(_AUTH_INSERT), {"connector_id": "global", "owner_id": None})
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(_AUTH_INSERT), {"connector_id": "own", "owner_id": "bob"})
    with engine.begin() as conn:
        conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text("DELETE FROM mcp_connectors WHERE id = 'own'"))
        assert conn.execute(sa.text("SELECT count(*) FROM mcp_connector_auth")).scalar() == 0

    with engine.begin() as conn:
        _downgrade(conn)
        inspector = sa.inspect(conn)
        assert not (_NEW_TABLES & set(inspector.get_table_names()))
        assert not any(
            u["name"] == "uq_connectors_id_owner" for u in inspector.get_unique_constraints("mcp_connectors")
        )
        assert conn.execute(sa.text("SELECT id FROM mcp_connectors")).scalars().all() == ["global"]


_CLIENT_INSERT = (
    "INSERT INTO mcp_oauth_clients (id, issuer, source, client_id, token_endpoint_auth_method, "
    "redirect_uri, retired_at, created_at, updated_at) VALUES (:id, 'https://as.example.com', 'dcr', "
    ":id, 'none', 'https://claw.example.com/cb', :retired_at, '2026-01-01', '2026-01-01')"
)


def test_one_live_registration_per_issuer_beside_any_number_retired(engine):
    with engine.begin() as conn:
        _upgrade(conn)
        conn.execute(sa.text(_CLIENT_INSERT), {"id": "old-1", "retired_at": "2026-01-02"})
        conn.execute(sa.text(_CLIENT_INSERT), {"id": "old-2", "retired_at": "2026-01-03"})
        conn.execute(sa.text(_CLIENT_INSERT), {"id": "live", "retired_at": None})
    with engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text(_CLIENT_INSERT), {"id": "second-live", "retired_at": None})


def test_migration_matches_the_orm_schema(engine):
    """The test suite and a fresh install's fallback build the schema with
    create_all, so the migration must not drift from the models."""

    def include_object(obj, name, type_, reflected, compare_to) -> bool:
        return type_ != "table" or name in _NEW_TABLES | {"mcp_connectors"}

    with engine.begin() as conn:
        _upgrade(conn)
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "include_object": include_object}
        )
        assert compare_metadata(context, Base.metadata) == []

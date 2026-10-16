"""번호순 추가 전용 마이그레이션 적용기. 적용 기록은 schema_migrations 테이블에 남긴다.

사용:  .venv/bin/python apply_migrations.py [--status]
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql

from dbtools.scram import scram_verifier

MIGRATION_FILE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
ADVISORY_LOCK_KEY = 727_274_001  # 동시에 두 곳에서 적용하지 못하게 하는 잠금


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


@dataclass
class Plan:
    pending: list[Migration] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def checksum_of(text: str) -> str:
    # Windows(CRLF)와 맥(LF) 체크아웃의 줄바꿈 차이로 오탐이 나지 않게 정규화한다.
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(p for p in Path(directory).iterdir() if p.is_file() and p.suffix == ".sql"):
        match = MIGRATION_FILE.match(path.name)
        if not match:
            raise MigrationError(f"invalid migration file name: {path.name}")
        text = path.read_text(encoding="utf-8")
        migrations.append(Migration(match.group(1), path.name, text, checksum_of(text)))

    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version")
    if versions != [f"{i:03d}" for i in range(1, len(versions) + 1)]:
        raise MigrationError("migration versions must be contiguous starting from 001")
    return migrations


def plan(migrations: list[Migration], applied: dict[str, str]) -> Plan:
    """적용 기록과 파일을 비교한다. 오류 메시지에는 SQL 내용을 담지 않는다."""
    by_version = {m.version: m for m in migrations}
    errors: list[str] = []
    for version, checksum in sorted(applied.items()):
        migration = by_version.get(version)
        if migration is None:
            errors.append(f"migration {version} is applied but its file is missing")
        elif migration.checksum != checksum:
            errors.append(f"migration {version} was modified after being applied (add a new file instead)")

    pending = [m for m in migrations if m.version not in applied]
    if applied and pending:
        latest = max(applied)
        errors += [
            f"migration {m.version} is not applied but a later one ({latest}) is"
            for m in pending
            if m.version < latest
        ]
    return Plan(pending=pending, errors=errors)


BOOTSTRAP_SQL = """
create table if not exists public.schema_migrations (
  version text primary key,
  checksum text not null,
  applied_at timestamptz not null default now()
);
alter table public.schema_migrations enable row level security;
revoke all on table public.schema_migrations from public, anon, authenticated;
"""


def read_applied(conn: psycopg.Connection) -> dict[str, str]:
    conn.execute(BOOTSTRAP_SQL)
    return dict(conn.execute("select version, checksum from public.schema_migrations").fetchall())


def apply(
    admin_dsn: str,
    migrations: list[Migration],
    role_passwords: dict[str, str],
    echo=print,
) -> Plan:
    """미적용 마이그레이션을 적용하고, 역할 비밀번호를 SCRAM 검증값으로 설정한다."""
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("select pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        try:
            result = plan(migrations, read_applied(conn))
            if result.errors:
                raise MigrationError("; ".join(result.errors))

            for migration in result.pending:
                with conn.transaction():  # 마이그레이션 하나는 전부 적용되거나 전부 취소된다
                    conn.execute(migration.sql)
                    conn.execute(
                        "insert into public.schema_migrations (version, checksum) values (%s, %s)",
                        (migration.version, migration.checksum),
                    )
                echo(f"applied {migration.name}")
            if not result.pending:
                echo("no pending migrations")

            for role, password in role_passwords.items():
                conn.execute(
                    sql.SQL("alter role {} password {}").format(
                        sql.Identifier(role), sql.Literal(scram_verifier(password))
                    )
                )
            echo(f"set password for roles: {', '.join(role_passwords)}")
            return result
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def status(admin_dsn: str, migrations: list[Migration], echo=print) -> Plan:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        result = plan(migrations, read_applied(conn))
    for migration in migrations:
        mark = "pending" if migration in result.pending else "applied"
        echo(f"{mark:8s} {migration.name}")
    for error in result.errors:
        echo(f"ERROR {error}")
    return result

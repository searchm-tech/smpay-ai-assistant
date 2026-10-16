"""sql/NNN_*.sql 을 번호순으로 적용한다.

  .venv/bin/python apply_migrations.py            # 미적용분 적용 + 역할 비밀번호 설정
  .venv/bin/python apply_migrations.py --status   # 변경 없이 상태만 확인

필요한 환경변수(레포 루트 .env, git 제외): DATABASE_ADMIN_URL, CHAT_READER_PASSWORD, INGEST_WRITER_PASSWORD
"""

import sys
from pathlib import Path

from dbtools.config import ConfigError, load_env, require_env, validate_password
from dbtools.migrate import MigrationError, apply, load_migrations, status

SQL_DIR = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    try:
        load_env()
        migrations = load_migrations(SQL_DIR)
        admin_dsn = require_env("DATABASE_ADMIN_URL")
        if "--status" in argv:
            return 1 if status(admin_dsn, migrations).errors else 0
        passwords = {
            "chat_reader": validate_password(require_env("CHAT_READER_PASSWORD"), "CHAT_READER_PASSWORD"),
            "ingest_writer": validate_password(require_env("INGEST_WRITER_PASSWORD"), "INGEST_WRITER_PASSWORD"),
        }
        apply(admin_dsn, migrations, passwords)
        return 0
    except (ConfigError, MigrationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

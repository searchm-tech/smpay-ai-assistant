import pytest

from dbtools.migrate import MigrationError, checksum_of, load_migrations, plan


def write(dir_, name, text="select 1;"):
    (dir_ / name).write_text(text, encoding="utf-8")


def test_load_returns_migrations_sorted_by_version(tmp_path):
    write(tmp_path, "002_b.sql")
    write(tmp_path, "001_a.sql")
    assert [m.version for m in load_migrations(tmp_path)] == ["001", "002"]


def test_load_ignores_non_sql_files(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "README.md")
    write(tmp_path, ".gitkeep")
    assert [m.version for m in load_migrations(tmp_path)] == ["001"]


@pytest.mark.parametrize("bad", ["1_x.sql", "001-x.sql", "001_X.sql", "init.sql", "0001_x.sql"])
def test_load_rejects_badly_named_sql_files(tmp_path, bad):
    write(tmp_path, bad)
    with pytest.raises(MigrationError):
        load_migrations(tmp_path)


def test_load_rejects_duplicate_versions(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "001_b.sql")
    with pytest.raises(MigrationError):
        load_migrations(tmp_path)


def test_load_rejects_gaps_in_numbering(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "003_c.sql")
    with pytest.raises(MigrationError):
        load_migrations(tmp_path)


def test_checksum_ignores_line_endings_so_windows_checkout_does_not_trigger_false_alarm():
    assert checksum_of("select 1;\nselect 2;\n") == checksum_of("select 1;\r\nselect 2;\r\n")


def test_checksum_changes_when_content_changes():
    assert checksum_of("select 1;") != checksum_of("select 2;")


def test_plan_marks_everything_pending_when_nothing_applied(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "002_b.sql")
    p = plan(load_migrations(tmp_path), applied={})
    assert [m.version for m in p.pending] == ["001", "002"]
    assert p.errors == []


def test_plan_skips_already_applied(tmp_path):
    write(tmp_path, "001_a.sql", "select 1;")
    write(tmp_path, "002_b.sql", "select 2;")
    migs = load_migrations(tmp_path)
    p = plan(migs, applied={"001": migs[0].checksum})
    assert [m.version for m in p.pending] == ["002"]
    assert p.errors == []


def test_plan_reports_modified_applied_migration_without_echoing_sql(tmp_path):
    write(tmp_path, "001_a.sql", "select 'SECRET-BODY';")
    migs = load_migrations(tmp_path)
    p = plan(migs, applied={"001": "0" * 64})
    assert p.errors and "001" in p.errors[0]
    assert "SECRET-BODY" not in " ".join(p.errors)


def test_plan_reports_applied_migration_whose_file_is_missing(tmp_path):
    write(tmp_path, "001_a.sql")
    migs = load_migrations(tmp_path)
    p = plan(migs, applied={"001": migs[0].checksum, "002": "x" * 64})
    assert any("002" in e for e in p.errors)


def test_plan_reports_out_of_order_pending_migration(tmp_path):
    write(tmp_path, "001_a.sql")
    write(tmp_path, "002_b.sql")
    migs = load_migrations(tmp_path)
    p = plan(migs, applied={"002": migs[1].checksum})  # 001이 안 적용됐는데 002가 적용됨
    assert any("001" in e for e in p.errors)

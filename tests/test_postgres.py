from baic_preflight.checks import BLOCKER, OK, PG_SQL, check_postgres


def runner(row):
    def run(dsn, sql):
        assert sql == PG_SQL
        return row
    return run


def test_good_postgres(spec):
    found = check_postgres("dsn", spec, runner("17.5 (Debian 17.5-1)|t|f"))
    assert [f.level for f in found] == [OK, OK]


def test_old_postgres_without_createdb(spec):
    found = check_postgres("dsn", spec, runner("16.4|f|f"))
    assert [f.level for f in found] == [BLOCKER, BLOCKER]
    assert "16.4" in found[0].message and "CREATEDB" in found[1].message


def test_superuser_counts_as_createdb(spec):
    assert check_postgres("dsn", spec, runner("17.4|f|t"))[1].level == OK


def test_connect_failure(spec):
    def boom(dsn, sql):
        raise RuntimeError("connection refused")
    (f,) = check_postgres("dsn", spec, boom)
    assert f.level == BLOCKER and "connection refused" in f.message

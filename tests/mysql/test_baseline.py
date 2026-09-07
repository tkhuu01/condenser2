"""Characterization, not a claim of parity with PostgreSQL."""

import pytest
from mysql.connector.errors import ProgrammingError

from db_condenser import mysql_database_helper as helper
from db_condenser.db_connect import DbConnect


class KnownRelationshipFailure(Exception):
    """Only the reproduced baseline failure may be treated as expected."""


def test_direct_target_filters_payload(mysql_case, mysql_run):
    (_, destination), config = mysql_case
    mysql_run()
    with destination.cursor() as cur:
        cur.execute("SELECT id, selected FROM parent ORDER BY id")
        assert cur.fetchall() == [(1, 1)]


def test_metadata_and_fk_discovery(mysql_case):
    _, config = mysql_case
    dbc = DbConnect(config.db_type, config.source_db_connection_info)
    conn = dbc.get_db_connection()
    try:
        tables = [dbc.db_name + ".parent", dbc.db_name + ".child"]
        assert helper.get_table_columns("child", dbc.db_name, conn) == [
            "id",
            "parent_id",
            "payload",
        ]
        assert helper.get_unredacted_fk_relationships(tables, conn) == [
            {
                "fk_table": tables[1],
                "fk_columns": ["parent_id"],
                "target_table": tables[0],
                "target_columns": ["id"],
            }
        ]
    finally:
        conn.close()


@pytest.mark.xfail(
    strict=True,
    raises=KnownRelationshipFailure,
    reason="main: MySQL relationship paths use PostgreSQL array parameters or temp-table quoting",
)
@pytest.mark.parametrize(
    "use_temp_tables", [False, True], ids=["arrays", "temp_tables"]
)
def test_upstream_relationship_selection(mysql_case, mysql_run, use_temp_tables):
    (_, destination), config = mysql_case
    config.use_temp_tables = use_temp_tables
    try:
        mysql_run(relationships=True)
    except Exception as error:
        array_failure = (
            not use_temp_tables
            and type(error).__name__ == "MySQLInterfaceError"
            and str(error) == "Python type list cannot be converted"
        )
        temp_failure = (
            use_temp_tables
            and isinstance(error, ProgrammingError)
            and error.errno == 1064
            and '"condenser_baseline_' in str(error)
        )
        if array_failure or temp_failure:
            raise KnownRelationshipFailure(str(error)) from error
        raise
    with destination.cursor() as cur:
        cur.execute("SELECT id, parent_id, payload FROM child ORDER BY id")
        assert cur.fetchall() == [(10, 1, "accepted")]

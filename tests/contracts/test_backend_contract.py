"""Identical assertions for the behavior currently shared by both backends.

Backend-specific features and known relationship traversal failures stay in
their existing integration suites; this is not a claim of feature parity.
"""

import pytest

pytestmark = pytest.mark.integration


def rows(connection):
    with connection.cursor() as cur:
        cur.execute("SELECT * FROM child ORDER BY id")
        return cur.fetchall()


def test_metadata_preserves_composite_relationship_order(backend_case):
    case = backend_case
    tables = [case.source_schema + "." + name for name in ("parent", "child")]
    assert set(case.backend.list_all_tables(case.source_factory)) == set(tables)
    assert case.backend.get_unredacted_fk_relationships(tables, case.source) == [
        {
            "fk_table": tables[1],
            "fk_columns": ["parent_tenant", "parent_code"],
            "target_table": tables[0],
            "target_columns": ["tenant", "code"],
        }
    ]
    columns = ["id", "parent_tenant", "parent_code", "payload"]
    assert (
        case.backend.get_table_columns("child", case.source_schema, case.source)
        == columns
    )
    datatypes = case.backend.get_table_datatypes(
        "child", case.source_schema, case.source
    )
    assert [column[0] for column in datatypes] == columns
    assert all(isinstance(column, tuple) and len(column) == 4 for column in datatypes)
    assert all(column[1] and column[2:] == ("", "") for column in datatypes)
    assert isinstance(
        case.backend.get_table_count_estimate("child", case.source_schema, case.source),
        int,
    )
    assert len(rows(case.source)) == 2  # borrowed connection still usable


@pytest.mark.parametrize(
    "batch_size", [None, 1], ids=["default_batch", "single_row_batch"]
)
def test_copy_filters_and_commits_destination_not_source(backend_case, batch_size):
    case = backend_case
    with case.source.cursor() as cur:
        cur.execute("INSERT INTO child VALUES (30,1,'alpha','uncommitted')")
    case.backend.copy_rows(
        case.source,
        case.destination,
        "SELECT * FROM child WHERE parent_tenant = %s ORDER BY id",
        case.destination_schema + ".child",
        params=(1,),
        batch_size=batch_size,
    )
    case.source.connection.rollback()
    assert rows(case.source) == [
        (10, 1, "alpha", "accepted"),
        (20, 2, "beta", "rejected"),
    ]
    expected = [(10, 1, "alpha", "accepted"), (30, 1, "alpha", "uncommitted")]
    assert rows(case.destination) == expected
    assert (
        rows(case.observe_destination()) == expected
    )  # commit is visible outside the session
    case.backend.copy_rows(
        case.source,
        case.destination,
        "SELECT * FROM child WHERE id = %s",
        case.destination_schema + ".child",
        params=(-1,),
    )
    assert rows(case.destination) == expected  # empty selection is harmless


def test_hook_respects_commit_flag(backend_case):
    case = backend_case
    case.backend.run_query(
        "INSERT INTO child VALUES (40,1,'alpha','rolled back')",
        case.destination,
        commit=False,
    )
    case.destination.connection.rollback()
    assert rows(case.destination) == []
    case.backend.run_query(
        "INSERT INTO child VALUES (50,1,'alpha','committed')", case.destination
    )
    assert rows(case.observe_destination()) == [(50, 1, "alpha", "committed")]

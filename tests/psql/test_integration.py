import json
import os
from pathlib import Path

import psycopg
import pytest

from db_condenser import config_reader, database_helper
from db_condenser.db_connect import DbConnect, PsqlConnection
from db_condenser.direct_subset import db_creator
from db_condenser.subset import Subset

TEST_DIR = Path(__file__).parent
SEED_SQL = TEST_DIR / "seed.sql"
CONFIG_JSON = TEST_DIR / "test_config.json"

DB_USER = os.environ.get("POSTGRES_USER", "test")
DB_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "test")
DB_HOST = os.environ.get("POSTGRES_HOST", "localhost")
DB_PORT = os.environ.get("POSTGRES_PORT", "5432")

SOURCE_DB = "condenser_test_source"
DEST_DB = "condenser_test_dest"


def _admin_conn(dbname="postgres"):
    return psycopg.connect(
        dbname=dbname,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        autocommit=True,
    )


def _query_one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


def _run_subsetter(
    use_temp_tables: bool,
    use_copy_protocol: bool = False,
    skip_schema_setup: bool = False,
    suffix_override: str | None = None,
    parallel_read_workers: int = 1,
) -> tuple[str, str]:
    if suffix_override is not None:
        suffix = suffix_override
    else:
        suffix = "_temp" if use_temp_tables else "_copy" if use_copy_protocol else ""
    source_db = SOURCE_DB + suffix
    dest_db = DEST_DB + suffix

    if not skip_schema_setup:
        admin = _admin_conn()
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {source_db}")
            cur.execute(f"DROP DATABASE IF EXISTS {dest_db}")
            cur.execute(f"CREATE DATABASE {source_db}")
            cur.execute(f"CREATE DATABASE {dest_db}")
        admin.close()

        source_admin = _admin_conn(source_db)
        with source_admin.cursor() as cur:
            cur.execute(SEED_SQL.read_text())
        source_admin.close()

    with open(CONFIG_JSON, "r") as fp:
        raw_config = json.load(fp)
    raw_config["source_db_connection_info"]["db_name"] = source_db
    raw_config["destination_db_connection_info"]["db_name"] = dest_db
    raw_config["use_temp_tables"] = use_temp_tables
    raw_config["use_copy_protocol"] = use_copy_protocol
    raw_config["skip_schema_setup"] = skip_schema_setup
    raw_config["parallel_read_workers"] = parallel_read_workers

    config_reader.reset_config()
    config_reader.config = config_reader._raw_dict_to_config(raw_config)

    config = config_reader.get_config()
    db_type = config.db_type
    source_dbc = DbConnect(db_type, config.source_db_connection_info)
    destination_dbc = DbConnect(db_type, config.destination_db_connection_info)

    database = db_creator(db_type, source_dbc, destination_dbc)
    if not skip_schema_setup:
        database.teardown()
        database.create()

    db_helper = database_helper.get_specific_helper()
    all_tables = db_helper.list_all_tables(source_dbc)
    all_tables = [x for x in all_tables if x not in config.excluded_tables]

    subsetter = Subset(source_dbc, destination_dbc, all_tables)
    try:
        subsetter.prep_temp_dbs()
        subsetter.run_middle_out()

        if not skip_schema_setup:
            for sql_stmt in config.pre_constraint_sql:
                db_helper.run_query(sql_stmt, destination_dbc.get_db_connection())

            database.add_constraints()

            for sql_stmt in config.post_subset_sql:
                db_helper.run_query(sql_stmt, destination_dbc.get_db_connection())

            all_tables_no_pg = [t for t in all_tables if "pgbench" not in t]
            dest_conn = destination_dbc.get_db_connection()
            assert isinstance(dest_conn, PsqlConnection)
            db_helper.update_sequence_numbering(dest_conn, all_tables_no_pg)
    finally:
        subsetter.unprep_temp_dbs()
        subsetter.close_connections()

    return source_db, dest_db


@pytest.fixture(
    scope="module",
    params=[
        {"use_temp_tables": False, "use_copy_protocol": False},
        {"use_temp_tables": True, "use_copy_protocol": False},
        {"use_temp_tables": False, "use_copy_protocol": True},
    ],
    ids=["unnest", "temp_tables", "copy_protocol"],
)
def subsetter_dbs(request):
    source_db, dest_db = _run_subsetter(**request.param)

    source = psycopg.connect(
        dbname=source_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    dest = psycopg.connect(
        dbname=dest_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

    yield source, dest

    source.close()
    dest.close()

    admin = _admin_conn()
    with admin.cursor() as cur:
        for db in (source_db, dest_db):
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
    admin.close()


def test_passthrough_table_fully_copied(subsetter_dbs):
    source, dest = subsetter_dbs
    source_count = _query_one(source, "SELECT COUNT(*) FROM public.regions")
    dest_count = _query_one(dest, "SELECT COUNT(*) FROM public.regions")
    assert dest_count == source_count == 5


def test_disconnected_table_copied(subsetter_dbs):
    source, dest = subsetter_dbs
    source_count = _query_one(source, "SELECT COUNT(*) FROM public.feature_flags")
    dest_count = _query_one(dest, "SELECT COUNT(*) FROM public.feature_flags")
    assert dest_count == source_count == 3


def test_initial_target_filtered(subsetter_dbs):
    source, dest = subsetter_dbs
    source_count = _query_one(source, "SELECT COUNT(*) FROM sales.customers")
    dest_count = _query_one(dest, "SELECT COUNT(*) FROM sales.customers")
    # Direct target is 5, but downstream may pull in more to satisfy FKs
    assert dest_count < source_count
    assert dest_count >= 5


def test_dependency_break_nullifies_fk(subsetter_dbs):
    _, dest = subsetter_dbs
    non_null = _query_one(
        dest,
        "SELECT COUNT(*) FROM sales.customers WHERE favorite_order_id IS NOT NULL",
    )
    assert non_null == 0


def test_upstream_orders_follow_customers(subsetter_dbs):
    _, dest = subsetter_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.customers c WHERE c.id = o.customer_id
        )
        """,
    )
    assert orphans == 0
    # Should have some orders (not empty)
    order_count = _query_one(dest, "SELECT COUNT(*) FROM sales.orders")
    assert order_count > 0


def test_upstream_order_lines_follow_orders(subsetter_dbs):
    _, dest = subsetter_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_lines ol
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.orders o WHERE o.id = ol.order_id
        )
        """,
    )
    assert orphans == 0
    line_count = _query_one(dest, "SELECT COUNT(*) FROM sales.order_lines")
    assert line_count > 0


def test_downstream_products_pulled_in(subsetter_dbs):
    _, dest = subsetter_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_lines ol
        WHERE NOT EXISTS (
            SELECT 1 FROM inventory.products p WHERE p.id = ol.product_id
        )
        """,
    )
    assert orphans == 0
    product_count = _query_one(dest, "SELECT COUNT(*) FROM inventory.products")
    assert 0 < product_count <= 20


def test_downstream_warehouses_pulled_in(subsetter_dbs):
    _, dest = subsetter_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.orders o
        WHERE o.warehouse_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM inventory.warehouses w WHERE w.id = o.warehouse_id
        )
        """,
    )
    assert orphans == 0
    wh_count = _query_one(dest, "SELECT COUNT(*) FROM inventory.warehouses")
    assert 0 < wh_count <= 5


def test_upstream_multi_fk_no_orphans(subsetter_dbs):
    """order_transfers has two FKs to orders (from_order_id, to_order_id).
    Verify no orphaned references after subsetting."""
    _, dest = subsetter_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_transfers t
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.orders o WHERE o.id = t.from_order_id
        )
        OR NOT EXISTS (
            SELECT 1 FROM sales.orders o WHERE o.id = t.to_order_id
        )
        """,
    )
    assert orphans == 0


def test_upstream_multi_fk_and_semantics(subsetter_dbs):
    """Only transfers where BOTH from_order and to_order are imported should
    be included (AND semantics, not OR)."""
    _, dest = subsetter_dbs
    count = _query_one(dest, "SELECT COUNT(*) FROM sales.order_transfers")
    assert count == 3


def test_upstream_order_lines_multi_fk_no_orphans(subsetter_dbs):
    """order_lines has FKs to orders and products (different target tables).
    Verifies grouped ID collection keeps separate groups distinct."""
    _, dest = subsetter_dbs
    order_orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_lines ol
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.orders o WHERE o.id = ol.order_id
        )
        """,
    )
    product_orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_lines ol
        WHERE NOT EXISTS (
            SELECT 1 FROM inventory.products p WHERE p.id = ol.product_id
        )
        """,
    )
    assert order_orphans == 0
    assert product_orphans == 0


def test_stock_levels_fk_integrity(subsetter_dbs):
    """stock_levels has a composite PK and FKs to both warehouses and products.
    Verifies downstream subsetting pulls in all referenced rows."""
    _, dest = subsetter_dbs
    warehouse_orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM inventory.stock_levels sl
        WHERE NOT EXISTS (
            SELECT 1 FROM inventory.warehouses w WHERE w.id = sl.warehouse_id
        )
        """,
    )
    product_orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM inventory.stock_levels sl
        WHERE NOT EXISTS (
            SELECT 1 FROM inventory.products p WHERE p.id = sl.product_id
        )
        """,
    )
    assert warehouse_orphans == 0
    assert product_orphans == 0


def test_destination_has_fewer_rows(subsetter_dbs):
    source, dest = subsetter_dbs
    for table in ["sales.customers", "sales.orders", "sales.order_lines"]:
        src = _query_one(source, f"SELECT COUNT(*) FROM {table}")
        dst = _query_one(dest, f"SELECT COUNT(*) FROM {table}")
        assert dst < src, (
            f"{table}: dest ({dst}) should have fewer rows than source ({src})"
        )


def test_sequences_reset(subsetter_dbs):
    _, dest = subsetter_dbs
    tables_with_serials = [
        ("sales", "customers"),
        ("sales", "orders"),
        ("sales", "order_lines"),
        ("sales", "order_transfers"),
        ("inventory", "products"),
        ("inventory", "warehouses"),
    ]
    for schema, table in tables_with_serials:
        seq_name = _query_one(
            dest,
            f"SELECT pg_get_serial_sequence('{schema}.{table}', 'id')",
        )
        if seq_name is None:
            continue
        seq_val = _query_one(dest, f"SELECT last_value FROM {seq_name}")
        max_id = _query_one(dest, f"SELECT COALESCE(MAX(id), 0) FROM {schema}.{table}")
        assert seq_val >= max_id, (
            f"{schema}.{table}: sequence value ({seq_val}) should be >= max id ({max_id})"
        )


@pytest.fixture(
    scope="module",
    params=[
        {
            "use_temp_tables": False,
            "use_copy_protocol": False,
            "suffix_override": "_rerun",
        },
        {
            "use_temp_tables": True,
            "use_copy_protocol": False,
            "suffix_override": "_rerun_temp_tables",
        },
        {
            "use_temp_tables": False,
            "use_copy_protocol": True,
            "suffix_override": "_rerun_copy",
        },
    ],
    ids=["unnest_rerun", "temp_tables_rerun", "copy_protocol_rerun"],
)
def rerun_dbs(request):
    """Run the subsetter twice on the same destination with skip_schema_setup."""
    source_db, dest_db = _run_subsetter(**request.param)
    _run_subsetter(**request.param, skip_schema_setup=True)

    dest = psycopg.connect(
        dbname=dest_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    yield dest
    dest.close()

    admin = _admin_conn()
    with admin.cursor() as cur:
        for db in (source_db, dest_db):
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
    admin.close()


def test_rerun_no_duplicate_rows(rerun_dbs):
    dest = rerun_dbs
    for table in ["sales.customers", "sales.orders", "sales.order_lines"]:
        total = _query_one(dest, f"SELECT COUNT(*) FROM {table}")
        distinct = _query_one(dest, f"SELECT COUNT(DISTINCT id) FROM {table}")
        assert total == distinct, f"{table}: found duplicate rows after rerun"


def test_rerun_fk_integrity(rerun_dbs):
    dest = rerun_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.customers c WHERE c.id = o.customer_id
        )
        """,
    )
    assert orphans == 0


@pytest.fixture(
    scope="module",
    params=[
        {"use_copy_protocol": False, "suffix_override": "_parallel"},
        {"use_copy_protocol": True, "suffix_override": "_parallel_copy"},
    ],
    ids=["parallel_unnest", "parallel_copy_protocol"],
)
def parallel_dbs(request):
    """Run subsetter with parallel ctid page-range splitting."""
    source_db, dest_db = _run_subsetter(
        use_temp_tables=False,
        use_copy_protocol=request.param["use_copy_protocol"],
        parallel_read_workers=4,
        suffix_override=request.param["suffix_override"],
    )

    source = psycopg.connect(
        dbname=source_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    dest = psycopg.connect(
        dbname=dest_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

    yield source, dest

    source.close()
    dest.close()

    admin = _admin_conn()
    with admin.cursor() as cur:
        for db in (source_db, dest_db):
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
    admin.close()


def test_parallel_target_filtered(parallel_dbs):
    source, dest = parallel_dbs
    source_count = _query_one(source, "SELECT COUNT(*) FROM sales.customers")
    dest_count = _query_one(dest, "SELECT COUNT(*) FROM sales.customers")
    assert dest_count < source_count
    assert dest_count >= 5


def test_parallel_no_duplicate_rows(parallel_dbs):
    _, dest = parallel_dbs
    for table in ["sales.customers", "sales.orders", "sales.order_lines"]:
        total = _query_one(dest, f"SELECT COUNT(*) FROM {table}")
        distinct = _query_one(dest, f"SELECT COUNT(DISTINCT id) FROM {table}")
        assert total == distinct, f"{table}: duplicate rows with parallel reads"


def test_parallel_fk_integrity(parallel_dbs):
    _, dest = parallel_dbs
    orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM sales.customers c WHERE c.id = o.customer_id
        )
        """,
    )
    assert orphans == 0


@pytest.fixture(scope="module")
def pre_filter_dbs():
    """Run subsetter with a pre_filter to simulate FDW-based filtering."""
    source_db = SOURCE_DB + "_prefilter"
    dest_db = DEST_DB + "_prefilter"

    admin = _admin_conn()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {source_db}")
        cur.execute(f"DROP DATABASE IF EXISTS {dest_db}")
        cur.execute(f"CREATE DATABASE {source_db}")
        cur.execute(f"CREATE DATABASE {dest_db}")
    admin.close()

    source_admin = _admin_conn(source_db)
    with source_admin.cursor() as cur:
        cur.execute(SEED_SQL.read_text())
    source_admin.close()

    with open(CONFIG_JSON, "r") as fp:
        raw_config = json.load(fp)
    raw_config["source_db_connection_info"]["db_name"] = source_db
    raw_config["destination_db_connection_info"]["db_name"] = dest_db
    raw_config["initial_targets"] = [
        {"table": "sales.customers", "where": "1=1", "pre_filter": "region_ids"}
    ]
    raw_config["pre_filters"] = [
        {
            "name": "region_ids",
            "query": "SELECT id FROM sales.customers WHERE region = 'US-W'",
            "column": "id",
        }
    ]
    raw_config["parallel_read_workers"] = 4

    config_reader.reset_config()
    config_reader.config = config_reader._raw_dict_to_config(raw_config)

    config = config_reader.get_config()
    db_type = config.db_type
    source_dbc = DbConnect(db_type, config.source_db_connection_info)
    destination_dbc = DbConnect(db_type, config.destination_db_connection_info)

    database = db_creator(db_type, source_dbc, destination_dbc)
    database.teardown()
    database.create()

    db_helper = database_helper.get_specific_helper()
    all_tables = db_helper.list_all_tables(source_dbc)
    all_tables = [x for x in all_tables if x not in config.excluded_tables]

    subsetter = Subset(source_dbc, destination_dbc, all_tables)
    try:
        subsetter.prep_temp_dbs()
        subsetter.run_middle_out()
    finally:
        subsetter.unprep_temp_dbs()
        subsetter.close_connections()

    dest = psycopg.connect(
        dbname=dest_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

    yield dest

    dest.close()

    admin = _admin_conn()
    with admin.cursor() as cur:
        for db in (source_db, dest_db):
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
    admin.close()


def test_pre_filter_limits_rows(pre_filter_dbs):
    dest = pre_filter_dbs
    count = _query_one(dest, "SELECT COUNT(*) FROM sales.customers")
    # Only US-W customers: Alice(1), Eve(5), Iris(9) = 3
    assert count == 3
    regions = _query_one(
        dest,
        "SELECT COUNT(DISTINCT region) FROM sales.customers",
    )
    assert regions == 1

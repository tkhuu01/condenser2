import os
from pathlib import Path

import psycopg
import pytest

from condenser2 import config_reader, database_helper
from condenser2.db_connect import DbConnect, PsqlConnection
from condenser2.direct_subset import db_creator
from condenser2.subset import Subset

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


def _query_all(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


@pytest.fixture(scope="module")
def subsetter_dbs():
    admin = _admin_conn()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SOURCE_DB}")
        cur.execute(f"DROP DATABASE IF EXISTS {DEST_DB}")
        cur.execute(f"CREATE DATABASE {SOURCE_DB}")
        cur.execute(f"CREATE DATABASE {DEST_DB}")
    admin.close()

    source_admin = _admin_conn(SOURCE_DB)
    with source_admin.cursor() as cur:
        cur.execute(SEED_SQL.read_text())
    source_admin.close()

    # Reset config_reader global state and initialize with test config
    config_reader._config = {}
    with open(CONFIG_JSON, "r") as fp:
        config_reader.initialize(fp)

    # Run the full subsetter pipeline
    db_type = config_reader.get_db_type()
    source_dbc = DbConnect(db_type, config_reader.get_source_db_connection_info())
    destination_dbc = DbConnect(
        db_type, config_reader.get_destination_db_connection_info()
    )

    database = db_creator(db_type, source_dbc, destination_dbc)
    database.teardown()
    database.create()

    db_helper = database_helper.get_specific_helper()
    all_tables = db_helper.list_all_tables(source_dbc)
    all_tables = [x for x in all_tables if x not in config_reader.get_excluded_tables()]

    subsetter = Subset(source_dbc, destination_dbc, all_tables)
    try:
        subsetter.prep_temp_dbs()
        subsetter.run_middle_out()

        for sql in config_reader.get_pre_constraint_sql():
            db_helper.run_query(sql, destination_dbc.get_db_connection())

        database.add_constraints()

        for sql in config_reader.get_post_subset_sql():
            db_helper.run_query(sql, destination_dbc.get_db_connection())

        all_tables_no_pg = [t for t in all_tables if "pgbench" not in t]
        dest_conn = destination_dbc.get_db_connection()
        assert isinstance(dest_conn, PsqlConnection)
        db_helper.update_sequence_numbering(dest_conn, all_tables_no_pg)
    finally:
        subsetter.unprep_temp_dbs()
        subsetter.close_connections()

    # Yield connections for assertions
    source = psycopg.connect(
        dbname=SOURCE_DB,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    dest = psycopg.connect(
        dbname=DEST_DB,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )

    yield source, dest

    source.close()
    dest.close()

    # Teardown
    admin = _admin_conn()
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {SOURCE_DB}")
        cur.execute(f"DROP DATABASE IF EXISTS {DEST_DB}")
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
    _, dest = subsetter_dbs
    count = _query_one(dest, "SELECT COUNT(*) FROM sales.customers")
    # Only customers with created_at >= '2025-01-01' (IDs 6-10)
    assert count == 5


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
        next_val = _query_one(dest, f"SELECT nextval('{seq_name}')")
        max_id = _query_one(dest, f"SELECT COALESCE(MAX(id), 0) FROM {schema}.{table}")
        assert next_val > max_id, (
            f"{schema}.{table}: nextval ({next_val}) should be > max id ({max_id})"
        )

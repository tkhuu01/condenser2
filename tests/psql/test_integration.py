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
    topup: bool = False,
    suffix_override: str | None = None,
    parallel_read_workers: int = 1,
) -> tuple[str, str]:
    if suffix_override is not None:
        suffix = suffix_override
    else:
        suffix = "_temp" if use_temp_tables else "_copy" if use_copy_protocol else ""
    source_db = SOURCE_DB + suffix
    dest_db = DEST_DB + suffix

    if not topup:
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
    raw_config["destination_mode"] = "topup" if topup else "recreate"
    raw_config["parallel_read_workers"] = parallel_read_workers

    config_reader.reset_config()
    config_reader.config = config_reader._raw_dict_to_config(raw_config)

    config = config_reader.get_config()
    db_type = config.db_type
    source_dbc = DbConnect(db_type, config.source_db_connection_info)
    destination_dbc = DbConnect(db_type, config.destination_db_connection_info)

    database = db_creator(db_type, source_dbc, destination_dbc)
    if not topup:
        database.teardown()
        database.create()

    db_helper = database_helper.get_specific_helper()
    all_tables = db_helper.list_all_tables(source_dbc)
    all_tables = [x for x in all_tables if x not in config.excluded_tables]

    subsetter = Subset(source_dbc, destination_dbc, all_tables)
    try:
        subsetter.prep_temp_dbs()
        subsetter.run_middle_out()

        for sql_stmt in config.pre_constraint_sql:
            db_helper.run_query(sql_stmt, destination_dbc.get_db_connection())

        if not topup:
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
    """Run the subsetter twice on the same destination in topup mode."""
    source_db, dest_db = _run_subsetter(**request.param)
    _run_subsetter(**request.param, topup=True)

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


# ============================================================
# INCREMENTAL (TOP-UP) RE-RUNS
# ============================================================


@pytest.fixture(
    scope="module",
    params=[
        {
            "use_temp_tables": False,
            "use_copy_protocol": False,
            "suffix_override": "_incr",
        },
        {
            "use_temp_tables": True,
            "use_copy_protocol": False,
            "suffix_override": "_incr_tt",
        },
        {
            "use_temp_tables": False,
            "use_copy_protocol": True,
            "suffix_override": "_incr_cp",
        },
    ],
    ids=["unnest_incremental", "temp_tables_incremental", "copy_protocol_incremental"],
)
def incremental_dbs(request):
    """Run once, add rows to the source, then re-run in topup mode.

    New source rows:
    - customer 11 (Kate, 2025) matches the target WHERE -> should arrive
    - order 31 belongs to Kate -> should arrive (children of new parents)
    - order 32 belongs to customer 6 (imported in run 1) -> should NOT
      arrive (top-up semantics: existing entities stay frozen)
    - order_lines for each follow their order
    - product 21 is referenced only by order 31's line -> downstream pull
    - transfer 31->16 pairs a new order with a run-1 order -> should arrive
      (multi-FK AND semantics against delta + full sets)
    - transfer 32->16 references a never-imported order -> should NOT arrive
    """
    source_db, dest_db = _run_subsetter(**request.param)

    src = psycopg.connect(
        dbname=source_db,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
    )
    with src.cursor() as cur:
        cur.execute(
            "INSERT INTO sales.customers (name, email, region, created_at)"
            " VALUES ('Kate', 'kate@example.com', 'US-W', '2025-09-01')"
        )
        cur.execute(
            "INSERT INTO inventory.products (name, price, metadata)"
            " VALUES ('Widget Z', 19.99, NULL)"
        )
        cur.execute(
            "INSERT INTO sales.orders (customer_id, warehouse_id, ordered_at)"
            " VALUES (11, 1, '2025-09-02'), (6, 2, '2025-09-03')"
        )
        cur.execute(
            "INSERT INTO sales.order_lines (order_id, product_id, quantity, unit_price)"
            " VALUES (31, 21, 1, 19.99), (32, 1, 1, 9.99)"
        )
        cur.execute(
            "INSERT INTO sales.order_transfers (from_order_id, to_order_id, reason)"
            " VALUES (31, 16, 'new to old'), (32, 16, 'not imported')"
        )
    src.commit()
    src.close()

    _run_subsetter(**request.param, topup=True)

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


def test_incremental_new_rows_arrive(incremental_dbs):
    """New target rows, their descendants, and new downstream references."""
    dest = incremental_dbs
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.customers WHERE id = 11") == 1
    # run 1 imported customers 6-10; Kate makes 6 total
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.customers") == 6
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.orders WHERE id = 31") == 1
    assert (
        _query_one(dest, "SELECT COUNT(*) FROM sales.order_lines WHERE order_id = 31")
        == 1
    )
    assert (
        _query_one(dest, "SELECT COUNT(*) FROM inventory.products WHERE id = 21") == 1
    )


def test_incremental_existing_entities_frozen(incremental_dbs):
    """Top-up semantics: new children of already-imported parents stay out."""
    dest = incremental_dbs
    # order 32 belongs to an already-imported customer: top-up leaves it out
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.orders WHERE id = 32") == 0
    assert (
        _query_one(dest, "SELECT COUNT(*) FROM sales.order_lines WHERE order_id = 32")
        == 0
    )
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.orders") == 16


def test_incremental_multi_fk_new_to_old(incremental_dbs):
    """Multi-FK AND semantics: new order paired with a run-1 order arrives."""
    dest = incremental_dbs
    assert (
        _query_one(
            dest,
            "SELECT COUNT(*) FROM sales.order_transfers"
            " WHERE from_order_id = 31 AND to_order_id = 16",
        )
        == 1
    )
    assert (
        _query_one(
            dest,
            "SELECT COUNT(*) FROM sales.order_transfers WHERE from_order_id = 32",
        )
        == 0
    )
    # run 1's three transfers plus the new one
    assert _query_one(dest, "SELECT COUNT(*) FROM sales.order_transfers") == 4


def test_incremental_integrity_and_cleanup(incremental_dbs):
    """No duplicates, no FK orphans, and the delta schema is dropped."""
    dest = incremental_dbs
    for table in ["sales.customers", "sales.orders", "sales.order_lines"]:
        total = _query_one(dest, f"SELECT COUNT(*) FROM {table}")
        distinct = _query_one(dest, f"SELECT COUNT(DISTINCT id) FROM {table}")
        assert total == distinct, f"{table}: duplicate rows after incremental run"
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
    line_orphans = _query_one(
        dest,
        """
        SELECT COUNT(*) FROM sales.order_lines ol
        WHERE NOT EXISTS (SELECT 1 FROM sales.orders o WHERE o.id = ol.order_id)
           OR NOT EXISTS (SELECT 1 FROM inventory.products p WHERE p.id = ol.product_id)
        """,
    )
    assert line_orphans == 0
    leftover = _query_one(
        dest,
        "SELECT COUNT(*) FROM pg_namespace WHERE nspname = '_condenser'",
    )
    assert leftover == 0


def test_skip_schema_setup_alias_maps_to_destination_mode():
    with open(CONFIG_JSON, "r") as fp:
        raw = json.load(fp)
    raw.pop("destination_mode", None)

    raw["skip_schema_setup"] = True
    cfg = config_reader._raw_dict_to_config(raw)
    assert cfg.destination_mode == config_reader.DestinationMode.TOPUP

    raw["skip_schema_setup"] = False
    cfg = config_reader._raw_dict_to_config(raw)
    assert cfg.destination_mode == config_reader.DestinationMode.RECREATE

    del raw["skip_schema_setup"]
    cfg = config_reader._raw_dict_to_config(raw)
    assert cfg.destination_mode == config_reader.DestinationMode.RECREATE


# ============================================================
# MULTI-FK PAIRS ACROSS ID-BATCH BOUNDARIES (regression)
# ============================================================


@pytest.fixture(scope="module")
def multi_fk_batch_dbs():
    """Subset a two-FKs-to-one-parent table with a tiny ID batch size.

    parent has 6 rows, link forms a ring (1-2, 2-3, ... 6-1). All parents are
    imported, so AND semantics require every link. With batches of 2 parent
    IDs, a streamed join that binds the same batch to both constraints drops
    every ring edge whose ends fall in different batches.
    """
    import db_condenser.subset as subset_mod

    source_db = SOURCE_DB + "_mfk"
    dest_db = DEST_DB + "_mfk"
    admin = _admin_conn()
    with admin.cursor() as cur:
        for db in (source_db, dest_db):
            cur.execute(f"DROP DATABASE IF EXISTS {db}")
            cur.execute(f"CREATE DATABASE {db}")
    admin.close()

    src = _admin_conn(source_db)
    with src.cursor() as cur:
        cur.execute("""
            CREATE TABLE parent (id INT PRIMARY KEY);
            CREATE TABLE link (
                id INT PRIMARY KEY,
                from_id INT NOT NULL REFERENCES parent(id),
                to_id   INT NOT NULL REFERENCES parent(id)
            );
            INSERT INTO parent SELECT generate_series(1, 6);
            INSERT INTO link VALUES
                (1, 1, 2), (2, 2, 3), (3, 3, 4),
                (4, 4, 5), (5, 5, 6), (6, 6, 1);
        """)
    src.close()

    raw_config = {
        "db_type": "postgres",
        "initial_targets": [{"table": "public.parent", "where": "id <= 6"}],
        "source_db_connection_info": {
            "user_name": DB_USER,
            "password": DB_PASSWORD,
            "host": DB_HOST,
            "db_name": source_db,
            "port": DB_PORT,
        },
        "destination_db_connection_info": {
            "user_name": DB_USER,
            "password": DB_PASSWORD,
            "host": DB_HOST,
            "db_name": dest_db,
            "port": DB_PORT,
        },
    }
    config_reader.reset_config()
    config_reader.config = config_reader._raw_dict_to_config(raw_config)
    config = config_reader.get_config()

    real_batch_size = subset_mod.compute_batch_size
    subset_mod.compute_batch_size = lambda column_count: 2
    try:
        source_dbc = DbConnect(config.db_type, config.source_db_connection_info)
        destination_dbc = DbConnect(
            config.db_type, config.destination_db_connection_info
        )
        database = db_creator(config.db_type, source_dbc, destination_dbc)
        database.teardown()
        database.create()
        db_helper = database_helper.get_specific_helper()
        all_tables = db_helper.list_all_tables(source_dbc)
        subsetter = Subset(source_dbc, destination_dbc, all_tables)
        try:
            subsetter.prep_temp_dbs()
            subsetter.run_middle_out()
        finally:
            subsetter.unprep_temp_dbs()
            subsetter.close_connections()
    finally:
        subset_mod.compute_batch_size = real_batch_size

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


def test_multi_fk_pairs_survive_batch_boundaries(multi_fk_batch_dbs):
    dest = multi_fk_batch_dbs
    assert _query_one(dest, "SELECT COUNT(*) FROM parent") == 6
    # every link's parents are imported, so every link must be included,
    # regardless of which ID batch each parent landed in
    assert _query_one(dest, "SELECT COUNT(*) FROM link") == 6

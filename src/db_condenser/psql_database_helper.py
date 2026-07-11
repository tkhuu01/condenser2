import hashlib
import os
import uuid
from dataclasses import asdict

from psycopg import sql
from psycopg.types.json import Json, set_json_loads

from db_condenser.config_reader import DestinationMode, get_config
from db_condenser.db_connect import PsqlConnection
from db_condenser.subset_utils import (
    columns_joined,
    columns_tupled,
    compute_batch_size,
    fully_qualified_table,
    quoter,
    schema_name,
    table_name,
)

set_json_loads(lambda s: s)

# Table shapes never change during a run (schema is created before subsetting;
# constraints added after don't alter columns), so metadata lookups are cached
# per database. Saves a catalog round trip per copy_rows call, which the
# streamed upstream/downstream paths invoke once per ID batch.
_metadata_cache: dict = {}


def _conn_cache_key(conn):
    # host+port included so same-named source and destination databases on
    # different servers don't share cache entries
    info = conn.connection.info
    return (info.host, info.port, info.dbname)


# Incremental (top-up) state: when destination_mode is "topup", each destination
# table with a primary key gets a delta table in the _condenser schema that
# records the PKs inserted during this run. Upstream subsetting joins against
# these deltas instead of full tables, so re-runs cost O(new rows).
DELTA_SCHEMA = "_condenser"
_incremental_deltas: dict = {}


def _prefixed_identifier(prefix, qualified_table):
    """Build '<prefix><schema>_<table>', hashing when it would exceed
    Postgres's 63-byte identifier limit (which truncates silently and could
    collide across long table names)."""
    name = prefix + qualified_table.replace(".", "_")
    if len(name) > 63:
        name = prefix + hashlib.md5(qualified_table.encode()).hexdigest()
    return name


def get_tables_primary_keys(tables, conn):
    """Map each fully-qualified table to its ordered PK column list ([] if none)."""
    q = """
        SELECT ns.nspname || '.' || cl.relname,
               array_agg(att.attname ORDER BY x.ord)
          FROM pg_index i
          JOIN pg_class cl ON cl.oid = i.indrelid
          JOIN pg_namespace ns ON ns.oid = cl.relnamespace
          JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ord) ON true
          JOIN pg_attribute att ON att.attrelid = cl.oid AND att.attnum = x.attnum
         WHERE i.indisprimary
         GROUP BY 1
    """
    with conn.cursor() as cur:
        cur.execute(q)
        pk_map = dict(cur.fetchall())
    return {t: pk_map.get(t, []) for t in tables}


def prep_incremental(conn, tables):
    _incremental_deltas.clear()
    with conn.cursor() as cur:
        cur.execute('DROP SCHEMA IF EXISTS "{}" CASCADE'.format(DELTA_SCHEMA))
        cur.execute('CREATE SCHEMA "{}"'.format(DELTA_SCHEMA))
    pk_map = get_tables_primary_keys(tables, conn)
    no_pk = []
    for t in tables:
        pk = pk_map.get(t) or []
        if not pk:
            no_pk.append(t)
            continue
        name = _prefixed_identifier("new_ids_", t)
        qualified = '"{}"."{}"'.format(DELTA_SCHEMA, name)
        q = "CREATE UNLOGGED TABLE {} AS SELECT {} FROM {} WITH NO DATA".format(
            qualified, columns_joined(pk), fully_qualified_table(t)
        )
        with conn.cursor() as cur:
            cur.execute(q)
        _incremental_deltas[t] = (qualified, pk)
    conn.commit()
    if no_pk:
        print(
            "WARNING: tables without a primary key are processed"
            " non-incrementally and may accumulate duplicate rows on re-runs: "
            + ", ".join(no_pk)
        )


def unprep_incremental(conn):
    _incremental_deltas.clear()
    with conn.cursor() as cur:
        cur.execute('DROP SCHEMA IF EXISTS "{}" CASCADE'.format(DELTA_SCHEMA))
    conn.commit()


def drop_fk_constraints(conn):
    """Capture and drop all FK constraints on the destination.

    Incremental runs load into a destination whose constraints are live
    (added at the end of the first run), but middle-out ordering inserts
    upstream rows before the downstream rows they reference. FKs are dropped
    for the duration of the run and restored afterwards; PKs and unique
    indexes stay so ON CONFLICT dedup keeps working. Returns the dropped
    definitions for restore_fk_constraints.
    """
    q = """
        SELECT ns.nspname, cl.relname, con.conname, pg_get_constraintdef(con.oid)
          FROM pg_constraint con
          JOIN pg_class cl ON cl.oid = con.conrelid
          JOIN pg_namespace ns ON ns.oid = cl.relnamespace
         WHERE con.contype = 'f'
           AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
    """
    with conn.cursor() as cur:
        cur.execute(q)
        fks = cur.fetchall()

    if fks:
        backup_dir = os.path.join(os.getcwd(), "SQL")
        os.makedirs(backup_dir, exist_ok=True)
        backup_path = os.path.join(backup_dir, "incremental_fk_backup.sql")
        with open(backup_path, "w") as fp:
            for nsp, rel, name, defn in fks:
                fp.write(
                    'ALTER TABLE "{}"."{}" ADD CONSTRAINT "{}" {};\n'.format(
                        nsp, rel, name, defn
                    )
                )

    with conn.cursor() as cur:
        for nsp, rel, name, _ in fks:
            cur.execute(
                'ALTER TABLE "{}"."{}" DROP CONSTRAINT "{}"'.format(nsp, rel, name)
            )
    conn.commit()
    return fks


def restore_fk_constraints(conn, fks):
    with conn.cursor() as cur:
        for nsp, rel, name, defn in fks:
            cur.execute(
                'ALTER TABLE "{}"."{}" ADD CONSTRAINT "{}" {}'.format(
                    nsp, rel, name, defn
                )
            )
    conn.commit()


def delta_for(table):
    """Return (qualified_delta_table, pk_columns) or None."""
    return _incremental_deltas.get(table)


def _wrap_insert_with_delta(insert_query, destination_table):
    delta = _incremental_deltas.get(destination_table)
    if not delta:
        return insert_query
    qualified, pk = delta
    return "WITH ins AS ({} RETURNING {}) INSERT INTO {} SELECT * FROM ins".format(
        insert_query, columns_joined(pk), qualified
    )


def prep_temp_dbs(_, __):
    # runs once at the start of every subset run: drop metadata cached from
    # any prior run in this process, in case a same-named database was
    # dropped and recreated with a different shape in between
    _metadata_cache.clear()


def unprep_temp_dbs(_, __):
    pass


def turn_off_constraints(connection):
    # can't be done in postgres
    pass


def copy_rows(
    source, destination, query, destination_table, params=None, batch_size=None
):
    datatypes = get_table_datatypes(
        table_name(destination_table), schema_name(destination_table), destination
    )
    if batch_size is None:
        batch_size = compute_batch_size(len(datatypes))

    non_generated_columns = [
        (dt[0], dt[1]) for _, dt in enumerate(datatypes) if dt[2] != "s"
    ]
    generated_columns_positions = {i for i, dt in enumerate(datatypes) if "s" in dt[2]}
    always_generated_id = any([dt[3] == "a" for dt in datatypes])

    def template_piece(dt):
        if dt == "_json":
            return "%s::json[]"
        elif dt == "_jsonb":
            return "%s::jsonb[]"
        else:
            return "%s"

    template = (
        "(" + ",".join([template_piece(dt[1]) for dt in non_generated_columns]) + ")"
    )
    columns = '("' + '","'.join([dt[0] for dt in non_generated_columns]) + '")'

    json_positions = {
        i for i, dt in enumerate(non_generated_columns) if dt[1] in ("json", "jsonb")
    }

    def _adapt_json(val):
        if val is None:
            return None
        if isinstance(val, bytes):
            val = val.decode("utf-8")
        return Json(val)

    def _adapt_row(row):
        if json_positions:
            return tuple(
                _adapt_json(val) if i in json_positions else val
                for i, val in enumerate(row)
            )
        return row

    cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
    cursor = source.cursor(name=cursor_name)
    # using the inner_cursor means we don't log all the noise
    destination_cursor = destination.cursor().inner_cursor
    try:
        cursor.execute(query, params)

        insert_query = "INSERT INTO {} {} VALUES {} ON CONFLICT DO NOTHING".format(
            fully_qualified_table(destination_table), columns, template
        )
        if always_generated_id:
            insert_query = "INSERT INTO {} {} OVERRIDING SYSTEM VALUE VALUES {} ON CONFLICT DO NOTHING".format(
                fully_qualified_table(destination_table), columns, template
            )
        insert_query = _wrap_insert_with_delta(insert_query, destination_table)

        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break

            if generated_columns_positions:
                updated_rows = (
                    _adapt_row(
                        tuple(
                            val
                            for i, val in enumerate(row)
                            if i not in generated_columns_positions
                        )
                    )
                    for row in rows
                )
            else:
                updated_rows = (_adapt_row(row) for row in rows)

            destination_cursor.executemany(insert_query, updated_rows)

    finally:
        destination_cursor.close()
        cursor.close()
        destination.commit()


def copy_rows_copy_protocol(
    source, destination, query, destination_table, params=None, batch_size=None
):
    # batch_size is accepted for interface parity with copy_rows (both are used
    # as self.__copy_rows) but is unused here: the COPY stream self-chunks.
    datatypes = get_table_datatypes(
        table_name(destination_table), schema_name(destination_table), destination
    )

    non_generated_columns = [dt[0] for _, dt in enumerate(datatypes) if dt[2] != "s"]
    column_list = ", ".join('"' + col + '"' for col in non_generated_columns)
    always_generated_id = any(dt[3] == "a" for dt in datatypes)
    dest_table = fully_qualified_table(destination_table)
    # deterministic name so batched calls for the same table reuse one
    # session-local staging table instead of CREATE/DROP catalog churn per call
    temp_table = '"{}"'.format(
        _prefixed_identifier("_copy_staging_", destination_table)
    )

    # On a recreate run the destination was just built from the pre-data
    # schema, so no unique indexes exist during load and ON CONFLICT cannot
    # dedup anything: the staging pass would be a pure second write of every
    # row. COPY straight into the target instead. Top-up runs keep staging
    # for real dedup and delta capture.
    direct_copy = get_config().destination_mode == DestinationMode.RECREATE

    source_cursor = source.cursor().inner_cursor
    dest_cursor = destination.cursor().inner_cursor
    try:
        if not direct_copy:
            dest_cursor.execute(
                "CREATE TEMPORARY TABLE IF NOT EXISTS {} (LIKE {} INCLUDING DEFAULTS)".format(
                    temp_table, dest_table
                )
            )
            dest_cursor.execute("TRUNCATE {}".format(temp_table))

        # Block-level COPY streaming: pipe raw COPY data straight from the source
        # into the destination, avoiding a per-row Python loop. Selecting just the
        # non-generated columns keeps the stream aligned with the target column
        # list (generated columns are excluded; the cap, joins, etc. live in query).
        # COPY FROM inserts supplied values into identity columns natively.
        copy_out = "COPY (SELECT {} FROM ({}) AS _src) TO STDOUT".format(
            column_list, query
        )
        copy_in = "COPY {} ({}) FROM STDIN".format(
            dest_table if direct_copy else temp_table, column_list
        )
        # psycopg yields one buffer per row; writing each individually caps
        # throughput on Python loop overhead (~2x), so coalesce into ~1MB
        # chunks before writing
        with source_cursor.copy(copy_out, params) as src_copy:
            with dest_cursor.copy(copy_in) as dest_copy:
                buf = bytearray()
                for data in src_copy:
                    buf += data
                    if len(buf) >= 1 << 20:
                        dest_copy.write(bytes(buf))
                        buf.clear()
                if buf:
                    dest_copy.write(bytes(buf))

        if not direct_copy:
            insert_query = (
                "INSERT INTO {} ({}){} SELECT {} FROM {} ON CONFLICT DO NOTHING".format(
                    dest_table,
                    column_list,
                    " OVERRIDING SYSTEM VALUE" if always_generated_id else "",
                    column_list,
                    temp_table,
                )
            )
            dest_cursor.execute(
                _wrap_insert_with_delta(insert_query, destination_table)
            )
            # release the staging rows' disk immediately; otherwise the last
            # result set per table lingers until the session closes
            dest_cursor.execute("TRUNCATE {}".format(temp_table))
    finally:
        dest_cursor.close()
        source_cursor.close()
        destination.commit()


def source_db_temp_table(target_table):
    return "tonic_subset_" + schema_name(target_table) + "_" + table_name(target_table)


def create_id_temp_table(conn, number_of_columns: int) -> str:
    table_name = "tonic_subset_" + str(uuid.uuid4())
    column_defs = ",\n".join(
        ["    col" + str(aye) + "  varchar" for aye in range(number_of_columns)]
    )
    q = 'CREATE TEMPORARY TABLE "{}" (\n {} \n)'.format(table_name, column_defs)
    with conn.cursor() as cursor:
        cursor.execute(q)
    return table_name


def copy_to_temp_table(conn, query, target_table, pk_columns=None):
    temp_table = fully_qualified_table(source_db_temp_table(target_table))
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TEMPORARY TABLE IF NOT EXISTS "
            + temp_table
            + " AS "
            + query
            + " LIMIT 0"
        )
        if pk_columns:
            query = query + " WHERE {} NOT IN (SELECT {} FROM {})".format(
                columns_tupled(pk_columns), columns_joined(pk_columns), temp_table
            )
        cur.execute("INSERT INTO " + temp_table + " " + query)
        conn.commit()


def clean_temp_table_cells(fk_table, fk_columns, target_table, target_columns, conn):
    fk_alias = "tonic_subset_398dhjr23_fk"
    target_alias = "tonic_subset_398dhjr23_target"

    fk_table = fully_qualified_table(source_db_temp_table(fk_table))
    target_table = fully_qualified_table(source_db_temp_table(target_table))
    assignment_list = ",".join(["{} = NULL".format(quoter(c)) for c in fk_columns])
    column_matching = " AND ".join(
        [
            "{}.{} = {}.{}".format(fk_alias, quoter(fc), target_alias, quoter(tc))
            for fc, tc in zip(fk_columns, target_columns)
        ]
    )
    q = "UPDATE {} {} SET {} WHERE NOT EXISTS (SELECT 1 FROM {} {} WHERE {})".format(
        fk_table, fk_alias, assignment_list, target_table, target_alias, column_matching
    )
    run_query(q, conn)


def get_unredacted_fk_relationships(tables: list[str], conn: PsqlConnection):
    q = """
        SELECT fk_nsp.nspname || '.' || fk_table AS fk_table,
        array_agg(fk_att.attname ORDER BY fk_att.attnum) AS fk_columns,
        tar_nsp.nspname || '.' || target_table AS target_table,
        array_agg(tar_att.attname ORDER BY fk_att.attnum) AS target_columns
    FROM (
        SELECT
            fk.oid AS fk_table_id,
            fk.relnamespace AS fk_schema_id,
            fk.relname AS fk_table,
            unnest(con.conkey) as fk_column_id,

            tar.oid AS target_table_id,
            tar.relnamespace AS target_schema_id,
            tar.relname AS target_table,
            unnest(con.confkey) as target_column_id,

            con.connamespace AS constraint_nsp,
            con.conname AS constraint_name

        FROM pg_constraint con
        JOIN pg_class fk ON con.conrelid = fk.oid
        JOIN pg_class tar ON con.confrelid = tar.oid
        WHERE con.contype = 'f'
    ) sub
    JOIN pg_attribute fk_att
      ON fk_att.attrelid = fk_table_id AND fk_att.attnum = fk_column_id
    JOIN pg_attribute tar_att
      ON tar_att.attrelid = target_table_id AND tar_att.attnum = target_column_id
    JOIN pg_namespace fk_nsp
      ON fk_schema_id = fk_nsp.oid
    JOIN pg_namespace tar_nsp
      ON target_schema_id = tar_nsp.oid
    GROUP BY 1, 3, sub.constraint_nsp, sub.constraint_name;
    """

    relationships = list()

    with conn.cursor() as cur:
        cur.execute(q)
        for row in cur.fetchall():
            d = dict()
            d["fk_table"] = row[0]
            d["fk_columns"] = row[1]
            d["target_table"] = row[2]
            d["target_columns"] = row[3]

            if d["fk_table"] in tables and d["target_table"] in tables:
                relationships.append(d)

    config = get_config()
    for fka in config.fk_augmentation:
        augment = asdict(fka)
        not_present = True
        for r in relationships:
            not_present = not_present and not all(
                [r[key] == augment[key] for key in r.keys()]
            )
            if not not_present:
                break

        if (
            augment["fk_table"] in tables
            and augment["target_table"] in tables
            and not_present
        ):
            relationships.append(augment)

    return relationships


def run_query(query, conn, commit=True):
    with conn.cursor() as cur:
        cur.execute(query)
        if commit:
            conn.commit()


def update_sequence_numbering(conn: PsqlConnection, tables: list[str]):
    with conn.cursor() as cur:
        for full_table in tables:
            schema_ = schema_name(full_table)
            if schema_ is None:
                schema_ = "public"
            table_ = table_name(full_table)
            col_seq_query = """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name   = %s
                  AND (
                        column_default LIKE 'nextval(%%'
                        OR is_identity = 'YES'
                  )
            """
            cur.execute(col_seq_query, (schema_, table_))
            cols = [row[0] for row in cur.fetchall()]
            if not cols:
                continue
            for col in cols:
                seq_update_query = sql.SQL("""
                    SELECT setval(
                        pg_get_serial_sequence({tbl_lit}, {col_lit}),
                        COALESCE(MAX({col_id}), 0) + 1,
                        false
                    )
                    FROM {tbl_id}
                """).format(
                    tbl_lit=sql.Literal(schema_ + "." + table_),
                    col_lit=sql.Literal(col),
                    col_id=sql.Identifier(col),
                    tbl_id=sql.Identifier(schema_, table_),
                )
                cur.execute(seq_update_query)
        conn.commit()


def get_table_count_estimate(table_name, schema, conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT reltuples::BIGINT AS count
              FROM pg_class
             WHERE oid=\'"{}"."{}"\'::regclass
             """.format(schema, table_name)
        )
        return cur.fetchone()[0]


def get_table_columns(table, schema, conn):
    cache_key = ("columns", _conn_cache_key(conn), schema, table)
    cached = _metadata_cache.get(cache_key)
    if cached is not None:
        return cached
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT attname
              FROM pg_attribute
             WHERE attrelid=\'"{}"."{}"\'::regclass
               AND attnum > 0
               AND NOT attisdropped
             ORDER BY attnum;""".format(schema, table)
        )
        result = [r[0] for r in cur.fetchall()]
    _metadata_cache[cache_key] = result
    return result


def list_all_user_schemas(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT nspname
              FROM pg_catalog.pg_namespace
             WHERE nspname NOT LIKE 'pg\_%'
               AND nspname != 'information_schema';
            """
        )
        return [r[0] for r in cur.fetchall()]


def list_all_tables(db_connect):
    conn = db_connect.get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT concat(concat(nsp.nspname,'.'),cls.relname)
              FROM pg_class cls
              JOIN pg_namespace nsp
                ON nsp.oid = cls.relnamespace
             WHERE nsp.nspname NOT IN ('information_schema', 'pg_catalog')
               AND cls.relkind = 'r';
        """)
        return [r[0] for r in cur.fetchall()]


def get_table_page_count(table, schema, conn):
    """Return the number of heap pages for a table from pg_class.

    Config-supplied table names may be unqualified (schema=None); those
    resolve via search_path, matching how fully_qualified_table builds the
    data queries.
    """
    regclass = '"{}"."{}"'.format(schema, table) if schema else '"{}"'.format(table)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT relpages FROM pg_class WHERE oid='{}'::regclass".format(regclass)
        )
        row = cur.fetchone()
    return row[0] if row else 0


def get_table_datatypes(table, schema, conn):
    cache_key = ("datatypes", _conn_cache_key(conn), schema, table)
    cached = _metadata_cache.get(cache_key)
    if cached is not None:
        return cached
    if not schema:
        table_clause = "cl.relname = '{}'".format(table)
    else:
        table_clause = "cl.relname = '{}' AND ns.nspname = '{}'".format(table, schema)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                att.attname,
                ty.typname,
                att.attgenerated,
                att.attidentity
              FROM pg_attribute att
              JOIN pg_class cl ON cl.oid = att.attrelid
              JOIN pg_type ty ON ty.oid = att.atttypid
              JOIN pg_namespace ns ON ns.oid = cl.relnamespace
             WHERE {} AND att.attnum > 0 AND
               NOT att.attisdropped
             ORDER BY att.attnum;
        """.format(table_clause)
        )

        result = [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    _metadata_cache[cache_key] = result
    return result


def truncate_table(target_table, conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE {}".format(target_table))
        conn.commit()

import uuid
from dataclasses import asdict

from psycopg import sql
from psycopg.types.json import Json, set_json_loads

from db_condenser.config_reader import get_config
from db_condenser.db_connect import PsqlConnection
from db_condenser.subset_utils import (
    columns_joined,
    columns_tupled,
    fully_qualified_table,
    quoter,
    redact_relationships,
    schema_name,
    table_name,
)

set_json_loads(lambda s: s)


def prep_temp_dbs(_, __):
    pass


def unprep_temp_dbs(_, __):
    pass


def turn_off_constraints(connection):
    # can't be done in postgres
    pass


def copy_rows(source, destination, query, destination_table, params=None):
    datatypes = get_table_datatypes(
        table_name(destination_table), schema_name(destination_table), destination
    )

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

        insert_query = "INSERT INTO {} {} VALUES {}".format(
            fully_qualified_table(destination_table), columns, template
        )
        if always_generated_id:
            insert_query = "INSERT INTO {} {} OVERRIDING SYSTEM VALUE VALUES {}".format(
                fully_qualified_table(destination_table), columns, template
            )

        fetch_row_count = 100000
        while True:
            rows = cursor.fetchmany(fetch_row_count)
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


def copy_rows_copy_protocol(source, destination, query, destination_table, params=None):
    datatypes = get_table_datatypes(
        table_name(destination_table), schema_name(destination_table), destination
    )

    non_generated_columns = [dt[0] for _, dt in enumerate(datatypes) if dt[2] != "s"]
    generated_columns_positions = {i for i, dt in enumerate(datatypes) if "s" in dt[2]}

    column_list = ", ".join('"' + col + '"' for col in non_generated_columns)
    copy_command = "COPY {} ({}) FROM STDIN".format(
        fully_qualified_table(destination_table), column_list
    )

    cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
    cursor = source.cursor(name=cursor_name)
    dest_cursor = destination.cursor().inner_cursor
    try:
        cursor.execute(query, params)

        with dest_cursor.copy(copy_command) as copy:
            fetch_row_count = 100000
            while True:
                rows = cursor.fetchmany(fetch_row_count)
                if not rows:
                    break

                if generated_columns_positions:
                    for row in rows:
                        copy.write_row(
                            tuple(
                                val
                                for i, val in enumerate(row)
                                if i not in generated_columns_positions
                            )
                        )
                else:
                    for row in rows:
                        copy.write_row(row)
    finally:
        dest_cursor.close()
        cursor.close()
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


def get_redacted_table_references(
    table_name: str, tables: list[str], conn: PsqlConnection
):
    relationships = get_unredacted_fk_relationships(tables, conn)
    redacted = redact_relationships(relationships)
    return [r for r in redacted if r["target_table"] == table_name]


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
        return [r[0] for r in cur.fetchall()]


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


def get_table_datatypes(table, schema, conn):
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

        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]


def truncate_table(target_table, conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE {}".format(target_table))
        conn.commit()

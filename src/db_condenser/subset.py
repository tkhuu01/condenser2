import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_condenser import database_helper
from db_condenser.config_reader import DbType, InitialTarget, get_config
from db_condenser.db_connect import DbConnect
from db_condenser.subset_utils import (
    columns_joined,
    columns_to_copy,
    columns_tupled,
    compute_disconnected_tables,
    compute_downstream_tables,
    compute_upstream_tables,
    fully_qualified_table,
    mysql_db_name_hack,
    print_progress,
    quoter,
    redact_relationships,
    schema_name,
    table_name,
    upstream_filter_match,
)
from db_condenser.topo_orderer import get_topological_order_by_tables

"""
A QUICK NOTE ON DEFINITIONS:

Foreign key relationships form a graph. We make sure all subsetting happens on DAGs.
Nodes in the DAG are tables, and FKs point from the table with a FK column to the table
with the PK column. In other words, tables with FKs are upstream of tables with PKs.

Sometimes we'll refer to tables as downstream or 'target' tables, because they are
targeted by foreign keys. We will also use upstream or 'fk' tables, because they
have foreign keys.

Generally speaking, tables downstream of other tables have their membership defined
by the requirements of their upstream tables. And tables upstream can be more flexible
about their membership vis-a-vis the downstream tables (i.e. upstream tables can decide
to include more or less).
"""


class Subset:
    def __init__(
        self,
        source_dbc: DbConnect,
        destination_dbc: DbConnect,
        all_tables: list[str],
        # clean_previous=True,
    ):
        self.__source_conn = source_dbc.get_db_connection(read_repeatable=True)
        self.__destination_conn = destination_dbc.get_db_connection()

        self.__all_tables = all_tables

        self.__db_helper = database_helper.get_specific_helper()

        self.__db_helper.turn_off_constraints(self.__destination_conn)
        self.config = get_config()

        if self.config.use_temp_tables:
            self.__check_source_writable()

    def __check_source_writable(self):
        if self.config.db_type == DbType.POSTGRES:
            with self.__source_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_is_in_recovery(),"
                    " has_database_privilege(current_user, current_database(), 'TEMP')"
                )
                is_replica, has_temp = cur.fetchone()
            if is_replica:
                raise RuntimeError(
                    "use_temp_tables is enabled but the source database is a"
                    " read replica (pg_is_in_recovery() = true)"
                )
            if not has_temp:
                raise RuntimeError(
                    "use_temp_tables is enabled but the source user lacks the"
                    " TEMP privilege on the source database"
                )
        elif self.config.db_type == DbType.MYSQL:
            with self.__source_conn.cursor() as cur:
                cur.execute("SELECT @@global.read_only")
                (read_only,) = cur.fetchone()
            if read_only:
                raise RuntimeError(
                    "use_temp_tables is enabled but the source database is"
                    " read-only (@@global.read_only = 1)"
                )

    def run_middle_out(self):
        passthrough_tables = self.config.passthrough_tables
        relationships = self.__db_helper.get_unredacted_fk_relationships(
            self.__all_tables, self.__source_conn
        )
        disconnected_tables = compute_disconnected_tables(
            self.config.initial_target_tables,
            passthrough_tables,
            self.__all_tables,
            relationships,
        )
        connected_tables = [
            table for table in self.__all_tables if table not in disconnected_tables
        ]
        order = get_topological_order_by_tables(relationships, connected_tables)
        order = list(order)

        # start by subsetting the direct targets
        print(
            "Beginning subsetting with these direct targets: "
            + str(self.config.initial_target_tables)
        )
        start_time = time.time()
        processed_tables = set()
        for idx, target in enumerate(self.config.initial_targets):
            print_progress(target, idx + 1, len(self.config.initial_targets))
            self.__subset_direct(target, relationships)
            processed_tables.add(target.table)
        print("Direct target tables completed in {}s".format(time.time() - start_time))

        # greedily grab rows with foreign keys to rows in the target strata
        upstream_tables = compute_upstream_tables(
            self.config.initial_target_tables, order
        )
        print(
            "Beginning greedy upstream subsetting with these tables: "
            + str(upstream_tables)
        )
        start_time = time.time()
        for idx, t in enumerate(upstream_tables):
            print_progress(t, idx + 1, len(upstream_tables))
            data_added = self.__subset_upstream(t, processed_tables, relationships)
            if data_added:
                processed_tables.add(t)
        print("Greedy subsettings completed in {}s".format(time.time() - start_time))

        # process pass-through tables concurrently, you need this before subset_downstream,
        # so you can get all required downstream rows
        print("Beginning pass-through tables (concurrent): " + str(passthrough_tables))
        start_time = time.time()
        self.__copy_tables_concurrent(passthrough_tables)
        print("Pass-through completed in {}s".format(time.time() - start_time))

        # use subset_downstream to get all supporting rows according to existing needs
        downstream_tables = compute_downstream_tables(
            passthrough_tables, disconnected_tables, order
        )
        print(
            "Beginning downstream subsetting with these tables: "
            + str(downstream_tables)
        )
        start_time = time.time()
        for idx, t in enumerate(downstream_tables):
            print_progress(t, idx + 1, len(downstream_tables))
            self.subset_downstream(t, relationships)
        print("Downstream subsetting completed in {}s".format(time.time() - start_time))

        if self.config.keep_disconnected_tables:
            # get all the data for tables in disconnected components (i.e. pass those tables through)
            print("Beginning disconnected tables: " + str(disconnected_tables))
            start_time = time.time()
            for idx, t in enumerate(disconnected_tables):
                print_progress(t, idx + 1, len(disconnected_tables))
                q = "SELECT * FROM {}".format(fully_qualified_table(t))
                self.__db_helper.copy_rows(
                    self.__source_conn,
                    self.__destination_conn,
                    q,
                    mysql_db_name_hack(t, self.__destination_conn),
                )
            print(
                "Disconnected tables completed in {}s".format(time.time() - start_time)
            )

    def prep_temp_dbs(self):
        self.__db_helper.prep_temp_dbs(self.__source_conn, self.__destination_conn)

    def unprep_temp_dbs(self):
        self.__db_helper.unprep_temp_dbs(self.__source_conn, self.__destination_conn)

    def close_connections(self):
        self.__source_conn.close()
        self.__destination_conn.close()

    def __copy_table_worker(self, table):
        q = "SELECT * FROM {}".format(fully_qualified_table(table))
        if self.config.max_rows_per_table is not None:
            q += " LIMIT {}".format(self.config.max_rows_per_table)
        self.__db_helper.copy_rows(
            self.__source_conn,
            self.__destination_conn,
            q,
            mysql_db_name_hack(table, self.__destination_conn),
        )

    def __copy_tables_concurrent(self, tables, max_workers=4):
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self.__copy_table_worker, t): t for t in tables}
            for idx, future in enumerate(as_completed(futures)):
                table = futures[future]
                print_progress(table, idx + 1, len(tables))
                future.result()  # raises if the worker failed

    def __stream_ids_to_source_temp(self, dest_query, columns):
        """Stream ID rows from a destination query into a temp table on the source.

        Returns the temp table name on the source connection.
        """
        id_temp = self.__db_helper.create_id_temp_table(
            self.__source_conn, len(columns)
        )
        insert_q = 'INSERT INTO "{}" VALUES ({})'.format(
            id_temp, ",".join(["%s"] * len(columns))
        )
        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        dest_cursor = self.__destination_conn.cursor(name=cursor_name, withhold=True)
        src_insert_cur = self.__source_conn.cursor()
        try:
            dest_cursor.execute(dest_query)
            fetch_row_count = 100000
            while True:
                rows = dest_cursor.fetchmany(fetch_row_count)
                if not rows:
                    break
                valid_rows = [row for row in rows if all(c is not None for c in row)]
                if valid_rows:
                    src_insert_cur.executemany(insert_q, valid_rows)
            self.__source_conn.commit()
        finally:
            src_insert_cur.close()
            dest_cursor.close()
        return id_temp

    def __build_temp_table_join(
        self, source_table, id_temp, columns, datatypes, select_expr=None
    ):
        """Build a SELECT ... JOIN query against a source temp table."""
        fqt = fully_qualified_table(source_table)
        if select_expr is None:
            select_expr = "{}.*".format(fqt)
        join_conditions = " AND ".join(
            '{}.{} = "{}".col{}::{}'.format(
                fqt, quoter(col), id_temp, i, datatypes[col]
            )
            for i, col in enumerate(columns)
        )
        return 'SELECT {} FROM {} JOIN "{}" ON {}'.format(
            select_expr, fqt, id_temp, join_conditions
        )

    def __subset_direct(self, target: InitialTarget, relationships):
        t = target.table
        columns_query = columns_to_copy(t, relationships, self.__source_conn)
        if target.where is not None:
            q = "SELECT {} FROM {} WHERE {}".format(
                columns_query, fully_qualified_table(t), target.where
            )
        elif target.percent is not None:
            if self.config.db_type == DbType.POSTGRES:
                q = "SELECT {} FROM {} WHERE random() < {}".format(
                    columns_query,
                    fully_qualified_table(t),
                    float(target.percent) / 100,
                )
            else:
                q = "SELECT {} FROM {} WHERE rand() < {}".format(
                    columns_query,
                    fully_qualified_table(t),
                    float(target.percent) / 100,
                )
        else:
            raise ValueError(
                "target table {} had no 'where' or 'percent' term defined, check your configuration.".format(
                    t
                )
            )
        self.__db_helper.copy_rows(
            self.__source_conn,
            self.__destination_conn,
            q,
            mysql_db_name_hack(t, self.__destination_conn),
        )

    def __subset_upstream(self, target, processed_tables, relationships):
        redacted_relationships = redact_relationships(relationships)
        relevant_key_constraints = list(
            filter(
                lambda r: (
                    r["target_table"] in processed_tables and r["fk_table"] == target
                ),
                redacted_relationships,
            )
        )
        # this table isn't referenced by anything we've already processed, so let's leave it empty
        #  OR
        # table was already added, this only happens if the upstream table was also a direct target
        if len(relevant_key_constraints) == 0 or target in processed_tables:
            return False

        table_columns = self.__db_helper.get_table_columns(
            table_name(target), schema_name(target), self.__source_conn
        )
        upstream_filters = upstream_filter_match(target, table_columns)

        if self.config.use_temp_tables:
            self.__subset_upstream_temp_tables(
                target, relevant_key_constraints, upstream_filters
            )
        else:
            self.__subset_upstream_unnest(
                target, relevant_key_constraints, upstream_filters
            )

        return True

    def __subset_upstream_temp_tables(
        self, target, relevant_key_constraints, upstream_filters
    ):
        target_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(target), schema_name(target), self.__source_conn
            )
        }
        for kc in relevant_key_constraints:
            qualified_table = fully_qualified_table(
                mysql_db_name_hack(kc["target_table"], self.__destination_conn)
            )
            dest_query = "SELECT {} FROM {}".format(
                columns_joined(kc["target_columns"]), qualified_table
            )
            id_temp = self.__stream_ids_to_source_temp(dest_query, kc["target_columns"])
            q = self.__build_temp_table_join(
                target, id_temp, kc["target_columns"], target_datatypes
            )
            if upstream_filters:
                q += " AND {}".format(" AND ".join(upstream_filters))
            if self.config.max_rows_per_table is not None:
                q += " LIMIT {}".format(self.config.max_rows_per_table)
            self.__db_helper.copy_rows(
                self.__source_conn, self.__destination_conn, q, target
            )

    def __subset_upstream_unnest(
        self, target, relevant_key_constraints, upstream_filters
    ):
        target_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(target), schema_name(target), self.__source_conn
            )
        }

        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        dest_cursor = self.__destination_conn.cursor(name=cursor_name, withhold=True)
        try:
            fetch_row_count = 100000
            for kc in relevant_key_constraints:
                qualified_table = fully_qualified_table(
                    mysql_db_name_hack(kc["target_table"], self.__destination_conn)
                )
                query = "SELECT {} FROM {}".format(
                    columns_joined(kc["target_columns"]), qualified_table
                )

                dest_cursor.execute(query)
                while True:
                    rows = dest_cursor.fetchmany(fetch_row_count)
                    if not rows:
                        break
                    valid_rows = [
                        row for row in rows if all(c is not None for c in row)
                    ]
                    if not valid_rows:
                        continue

                    cols = kc["target_columns"]
                    unnest_args = ", ".join(
                        "%s::{}[]".format(target_datatypes[col]) for col in cols
                    )
                    join_cols = ", ".join("col{}".format(i) for i in range(len(cols)))
                    join_conditions = " AND ".join(
                        "{}.{} = ids.col{}".format(
                            fully_qualified_table(target), quoter(col), i
                        )
                        for i, col in enumerate(cols)
                    )
                    q = (
                        "SELECT {tbl}.* FROM {tbl}"
                        " JOIN unnest({unnest}) AS ids({join_cols})"
                        " ON {conditions}"
                    ).format(
                        tbl=fully_qualified_table(target),
                        unnest=unnest_args,
                        join_cols=join_cols,
                        conditions=join_conditions,
                    )
                    if upstream_filters:
                        q += " AND {}".format(
                            " AND ".join(upstream_filters),
                        )
                    if self.config.max_rows_per_table is not None:
                        q += " LIMIT {}".format(self.config.max_rows_per_table)

                    params = [[row[i] for row in valid_rows] for i in range(len(cols))]
                    self.__db_helper.copy_rows(
                        self.__source_conn,
                        self.__destination_conn,
                        q,
                        target,
                        params,
                    )
        finally:
            dest_cursor.close()

    def subset_downstream(self, table, relationships):
        """
        Table A -> Table B and Table A has the column b_id.  So we SELECT b_id
        from table_a from our destination database.  And we take those b_ids
        and run `select * from table b where id in (those list of ids)` then
        insert that result set into table b of the destination database
        """
        referencing_tables = self.__db_helper.get_redacted_table_references(
            table, self.__all_tables, self.__source_conn
        )

        if len(referencing_tables) > 0:
            pk_columns = referencing_tables[0]["target_columns"]
        else:
            print("Nothing to do in downstream subset")
            return

        temp_table = self.__db_helper.create_id_temp_table(
            self.__destination_conn, len(pk_columns)
        )

        for r in referencing_tables:
            fk_table = r["fk_table"]
            fk_columns = r["fk_columns"]

            q = "SELECT {} FROM {} WHERE {} NOT IN (SELECT {} FROM {})".format(
                columns_joined(fk_columns),
                fully_qualified_table(
                    mysql_db_name_hack(fk_table, self.__destination_conn)
                ),
                columns_tupled(fk_columns),
                columns_joined(pk_columns),
                fully_qualified_table(
                    mysql_db_name_hack(table, self.__destination_conn)
                ),
            )
            self.__db_helper.copy_rows(
                self.__destination_conn, self.__destination_conn, q, temp_table
            )

        columns_query = columns_to_copy(table, relationships, self.__source_conn)

        if self.config.use_temp_tables:
            self.__subset_downstream_temp_tables(
                table, temp_table, pk_columns, columns_query
            )
        else:
            self.__subset_downstream_unnest(
                table, temp_table, pk_columns, columns_query
            )

    def __subset_downstream_temp_tables(
        self, table, dest_temp_table, pk_columns, columns_query
    ):
        downstream_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(table), schema_name(table), self.__source_conn
            )
        }
        dest_query = "SELECT DISTINCT * FROM {}".format(
            fully_qualified_table(dest_temp_table)
        )
        src_id_temp = self.__stream_ids_to_source_temp(dest_query, pk_columns)
        q = self.__build_temp_table_join(
            table, src_id_temp, pk_columns, downstream_datatypes, columns_query
        )
        self.__db_helper.copy_rows(
            self.__source_conn,
            self.__destination_conn,
            q,
            mysql_db_name_hack(table, self.__destination_conn),
        )

    def __subset_downstream_unnest(
        self, table, dest_temp_table, pk_columns, columns_query
    ):
        downstream_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(table), schema_name(table), self.__source_conn
            )
        }

        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        cursor = self.__destination_conn.cursor(name=cursor_name, withhold=True)
        try:
            cursor_query = "SELECT DISTINCT * FROM {}".format(
                fully_qualified_table(dest_temp_table)
            )
            cursor.execute(cursor_query)
            fetch_row_count = 100000
            while True:
                rows = cursor.fetchmany(fetch_row_count)
                if not rows:
                    break
                valid_rows = [row for row in rows if all(c is not None for c in row)]
                if not valid_rows:
                    continue

                unnest_args = ", ".join(
                    "%s::{}[]".format(downstream_datatypes[col]) for col in pk_columns
                )
                join_cols = ", ".join("col{}".format(i) for i in range(len(pk_columns)))
                join_conditions = " AND ".join(
                    "{}.{} = ids.col{}".format(
                        fully_qualified_table(table), quoter(col), i
                    )
                    for i, col in enumerate(pk_columns)
                )
                q = (
                    "SELECT {cols} FROM {tbl}"
                    " JOIN unnest({unnest}) AS ids({join_cols})"
                    " ON {conditions}"
                ).format(
                    cols=columns_query,
                    tbl=fully_qualified_table(table),
                    unnest=unnest_args,
                    join_cols=join_cols,
                    conditions=join_conditions,
                )

                params = [
                    [row[i] for row in valid_rows] for i in range(len(pk_columns))
                ]
                self.__db_helper.copy_rows(
                    self.__source_conn,
                    self.__destination_conn,
                    q,
                    mysql_db_name_hack(table, self.__destination_conn),
                    params,
                )
        finally:
            cursor.close()

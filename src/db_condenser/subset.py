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
    compute_batch_size,
    compute_disconnected_tables,
    compute_downstream_strata,
    compute_upstream_strata,
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
    ):
        self.__source_dbc = source_dbc
        self.__destination_dbc = destination_dbc
        self.__source_conn = source_dbc.get_db_connection(read_repeatable=True)
        self.__destination_conn = destination_dbc.get_db_connection()

        self.__all_tables = all_tables

        self.__db_helper = database_helper.get_specific_helper()

        self.__db_helper.turn_off_constraints(self.__destination_conn)
        self.config = get_config()

        if self.config.use_copy_protocol and self.config.db_type == DbType.POSTGRES:
            self.__copy_rows = self.__db_helper.copy_rows_copy_protocol
        else:
            self.__copy_rows = self.__db_helper.copy_rows

        if self.config.use_temp_tables:
            self.__check_source_writable()

        self.__source_pool = []
        if (
            self.config.parallel_read_workers > 1
            and self.config.db_type == DbType.POSTGRES
        ):
            for _ in range(self.config.parallel_read_workers):
                self.__source_pool.append(
                    source_dbc.get_db_connection(read_repeatable=True)
                )

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

        # validate pre_filter references
        pf_names = {pf.name for pf in self.config.pre_filters}
        for target in self.config.initial_targets:
            if target.pre_filter and target.pre_filter not in pf_names:
                raise ValueError(
                    "initial target '{}' references pre_filter '{}' which does not exist".format(
                        target.table, target.pre_filter
                    )
                )

        # execute pre_filters once and cache results
        self.__pre_filter_cache = {}
        for pf in self.config.pre_filters:
            with self.__source_conn.cursor() as cur:
                cur.execute(pf.query)
                values = list(set(row[0] for row in cur.fetchall()))
                self.__pre_filter_cache[pf.name] = values
                print(
                    "Pre-filter '{}' cached {} unique values".format(
                        pf.name, len(values)
                    )
                )

        # start by subsetting the direct targets
        print(
            "Beginning direct targets: " + ", ".join(self.config.initial_target_tables)
        )
        start_time = time.time()
        processed_tables = set()
        if (
            self.config.parallel_read_workers > 1
            and self.config.db_type == DbType.POSTGRES
        ):
            for idx, target in enumerate(self.config.initial_targets):
                print_progress(target, idx + 1, len(self.config.initial_targets))
                self.__subset_direct_parallel(target, relationships)
        elif len(self.config.initial_targets) >= 3:
            self.__subset_direct_concurrent(relationships)
        else:
            for idx, target in enumerate(self.config.initial_targets):
                print_progress(target, idx + 1, len(self.config.initial_targets))
                self.__subset_direct(target, relationships)
        for target in self.config.initial_targets:
            processed_tables.add(target.table)
        print("Direct targets completed in {:.1f}s".format(time.time() - start_time))

        # greedily grab rows with foreign keys to rows in the target strata
        upstream_strata = compute_upstream_strata(
            self.config.initial_target_tables, order
        )
        upstream_tables = [t for stratum in upstream_strata for t in stratum]
        print("Beginning upstream subsetting: " + ", ".join(upstream_tables))
        start_time = time.time()
        table_idx = 0
        for stratum in upstream_strata:
            added = self.__process_stratum_upstream(
                stratum,
                processed_tables,
                relationships,
                table_idx,
                len(upstream_tables),
            )
            processed_tables.update(added)
            table_idx += len(stratum)
        print(
            "Upstream subsetting completed in {:.1f}s".format(time.time() - start_time)
        )

        # process pass-through tables concurrently, you need this before subset_downstream,
        # so you can get all required downstream rows
        print("Beginning pass-through tables: " + ", ".join(passthrough_tables))
        start_time = time.time()
        self.__copy_tables_concurrent(passthrough_tables)
        print("Pass-through completed in {:.1f}s".format(time.time() - start_time))

        # use subset_downstream to get all supporting rows according to existing needs
        downstream_strata = compute_downstream_strata(
            passthrough_tables, disconnected_tables, order
        )
        downstream_tables = [t for stratum in downstream_strata for t in stratum]
        print("Beginning downstream subsetting: " + ", ".join(downstream_tables))
        start_time = time.time()
        table_idx = 0
        for stratum in downstream_strata:
            self.__process_stratum_downstream(
                stratum, relationships, table_idx, len(downstream_tables)
            )
            table_idx += len(stratum)
        print(
            "Downstream subsetting completed in {:.1f}s".format(
                time.time() - start_time
            )
        )

        if self.config.keep_disconnected_tables:
            # get all the data for tables in disconnected components (i.e. pass those tables through)
            print("Beginning disconnected tables: " + ", ".join(disconnected_tables))
            start_time = time.time()
            for idx, t in enumerate(disconnected_tables):
                print_progress(t, idx + 1, len(disconnected_tables))
                q = "SELECT * FROM {}".format(fully_qualified_table(t))
                self.__copy_rows(
                    self.__source_conn,
                    self.__destination_conn,
                    q,
                    mysql_db_name_hack(t, self.__destination_conn),
                )
            print(
                "Disconnected tables completed in {:.1f}s".format(
                    time.time() - start_time
                )
            )

    def prep_temp_dbs(self):
        self.__db_helper.prep_temp_dbs(self.__source_conn, self.__destination_conn)

    def unprep_temp_dbs(self):
        self.__db_helper.unprep_temp_dbs(self.__source_conn, self.__destination_conn)

    def close_connections(self):
        self.__source_conn.close()
        self.__destination_conn.close()
        for conn in self.__source_pool:
            conn.close()

    def __process_stratum_upstream(
        self, stratum, processed_tables, relationships, start_idx, total_count
    ):
        added = set()
        if len(stratum) <= 1:
            for t in stratum:
                print_progress(t, start_idx + 1, total_count)
                data_added = self.__subset_upstream(
                    t,
                    processed_tables,
                    relationships,
                    self.__source_conn,
                    self.__destination_conn,
                )
                if data_added:
                    added.add(t)
            return added

        def upstream_worker(table):
            source_conn = self.__source_dbc.get_db_connection(read_repeatable=True)
            dest_conn = self.__destination_dbc.get_db_connection()
            try:
                return self.__subset_upstream(
                    table, processed_tables, relationships, source_conn, dest_conn
                )
            finally:
                source_conn.close()
                dest_conn.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for idx, t in enumerate(stratum):
                print_progress(t, start_idx + idx + 1, total_count)
                futures[pool.submit(upstream_worker, t)] = t
            for future in as_completed(futures):
                t = futures[future]
                if future.result():
                    added.add(t)
        return added

    def __process_stratum_downstream(
        self, stratum, relationships, start_idx, total_count
    ):
        if len(stratum) <= 1:
            for t in stratum:
                print_progress(t, start_idx + 1, total_count)
                self.subset_downstream(
                    t, relationships, self.__source_conn, self.__destination_conn
                )
            return

        def downstream_worker(table):
            source_conn = self.__source_dbc.get_db_connection(read_repeatable=True)
            dest_conn = self.__destination_dbc.get_db_connection()
            try:
                self.subset_downstream(table, relationships, source_conn, dest_conn)
            finally:
                source_conn.close()
                dest_conn.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for idx, t in enumerate(stratum):
                print_progress(t, start_idx + idx + 1, total_count)
                futures[pool.submit(downstream_worker, t)] = t
            for future in as_completed(futures):
                future.result()

    def __copy_table_worker(self, table):
        q = "SELECT * FROM {}".format(fully_qualified_table(table))
        if self.config.max_rows_per_table is not None:
            q += " LIMIT {}".format(self.config.max_rows_per_table)
        self.__copy_rows(
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

    def __subset_direct_concurrent(self, relationships, max_workers=4):
        targets = self.config.initial_targets
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.__subset_direct, t, relationships): t for t in targets
            }
            for idx, future in enumerate(as_completed(futures)):
                target = futures[future]
                print_progress(target, idx + 1, len(targets))
                future.result()

    def __get_pre_filter_info(self, target: InitialTarget):
        """Return (column, values) for a target's pre_filter, or None."""
        if target.pre_filter is None:
            return None
        pf = next(
            (p for p in self.config.pre_filters if p.name == target.pre_filter), None
        )
        if pf is None:
            return None
        values = self.__pre_filter_cache.get(pf.name)
        if not values:
            return None
        return (pf.column, values)

    def __subset_direct_parallel(self, target: InitialTarget, relationships):
        """Subset a direct target using parallel ctid page-range splitting."""
        t = target.table
        num_workers = self.config.parallel_read_workers

        page_count = self.__db_helper.get_table_page_count(
            table_name(t), schema_name(t), self.__source_conn
        )
        if page_count < num_workers * 10:
            self.__subset_direct(target, relationships)
            return

        columns_query = columns_to_copy(t, relationships, self.__source_conn)
        fqt = fully_qualified_table(t)
        pages_per_worker = page_count // num_workers
        pre_filter_info = self.__get_pre_filter_info(target)

        def worker(idx, start_page, end_page):
            source_conn = self.__source_pool[idx]
            dest_conn = self.__destination_dbc.get_db_connection()
            try:
                ctid_filter = (
                    "{}.ctid >= '({},0)'::tid AND {}.ctid < '({},0)'::tid".format(
                        fqt, start_page, fqt, end_page
                    )
                )
                conditions = [ctid_filter]
                if target.where is not None:
                    conditions.append("({})".format(target.where))
                elif target.percent is not None:
                    conditions.append(
                        "random() < {}".format(float(target.percent) / 100)
                    )
                if pre_filter_info:
                    conditions.append(
                        '{}."{}" = ANY(%s)'.format(fqt, pre_filter_info[0])
                    )
                q = "SELECT {} FROM {} WHERE {}".format(
                    columns_query, fqt, " AND ".join(conditions)
                )
                params = [pre_filter_info[1]] if pre_filter_info else None
                self.__copy_rows(source_conn, dest_conn, q, t, params)
            finally:
                dest_conn.close()

        with ThreadPoolExecutor(max_workers=num_workers) as pool:
            futures = []
            for idx in range(num_workers):
                start_page = idx * pages_per_worker
                end_page = (
                    page_count
                    if idx == num_workers - 1
                    else (idx + 1) * pages_per_worker
                )
                futures.append(pool.submit(worker, idx, start_page, end_page))
            for future in as_completed(futures):
                future.result()

    def __stream_ids_to_source_temp(
        self, dest_query, columns, source_conn=None, dest_conn=None
    ):
        source_conn = source_conn or self.__source_conn
        dest_conn = dest_conn or self.__destination_conn
        id_temp = self.__db_helper.create_id_temp_table(source_conn, len(columns))
        insert_q = 'INSERT INTO "{}" VALUES ({})'.format(
            id_temp, ",".join(["%s"] * len(columns))
        )
        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        dest_cursor = dest_conn.cursor(name=cursor_name, withhold=True)
        src_insert_cur = source_conn.cursor()
        try:
            dest_cursor.execute(dest_query)
            batch_size = compute_batch_size(len(columns))
            while True:
                rows = dest_cursor.fetchmany(batch_size)
                if not rows:
                    break
                valid_rows = [row for row in rows if all(c is not None for c in row)]
                if valid_rows:
                    src_insert_cur.executemany(insert_q, valid_rows)
            source_conn.commit()
        finally:
            src_insert_cur.close()
            dest_cursor.close()
        return id_temp

    def __build_temp_table_join(
        self,
        source_table,
        id_temp,
        join_columns,
        datatypes,
        select_expr=None,
    ):
        """Build a SELECT ... JOIN query against a source temp table.

        join_columns are the columns on source_table to match against the temp table.
        datatypes maps temp table column names to their real types for casting.
        """
        fqt = fully_qualified_table(source_table)
        if select_expr is None:
            select_expr = "{}.*".format(fqt)
        join_conditions = " AND ".join(
            '{}.{} = "{}".col{}::{}'.format(
                fqt, quoter(col), id_temp, i, datatypes[col]
            )
            for i, col in enumerate(join_columns)
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
        pre_filter_info = self.__get_pre_filter_info(target)
        params = None
        if pre_filter_info:
            q += ' AND {}."{}" = ANY(%s)'.format(
                fully_qualified_table(t), pre_filter_info[0]
            )
            params = [pre_filter_info[1]]
        self.__copy_rows(
            self.__source_conn,
            self.__destination_conn,
            q,
            mysql_db_name_hack(t, self.__destination_conn),
            params,
        )

    def __subset_upstream(
        self, target, processed_tables, relationships, source_conn, dest_conn
    ):
        redacted_relationships = redact_relationships(relationships)
        relevant_key_constraints = list(
            filter(
                lambda r: (
                    r["target_table"] in processed_tables and r["fk_table"] == target
                ),
                redacted_relationships,
            )
        )
        if len(relevant_key_constraints) == 0 or target in processed_tables:
            return False

        table_columns = self.__db_helper.get_table_columns(
            table_name(target), schema_name(target), source_conn
        )
        upstream_filters = upstream_filter_match(target, table_columns)

        if self.config.use_temp_tables:
            self.__subset_upstream_temp_tables(
                target,
                relevant_key_constraints,
                upstream_filters,
                source_conn,
                dest_conn,
            )
        else:
            self.__subset_upstream_unnest(
                target,
                relevant_key_constraints,
                upstream_filters,
                source_conn,
                dest_conn,
            )

        return True

    def __subset_upstream_temp_tables(
        self,
        target,
        relevant_key_constraints,
        upstream_filters,
        source_conn,
        dest_conn,
    ):
        fk_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(target), schema_name(target), source_conn
            )
        }
        groups = {}
        for kc in relevant_key_constraints:
            key = (kc["target_table"], tuple(kc["target_columns"]))
            groups.setdefault(key, []).append(kc)

        group_temps = {}
        for kc_target, target_cols in groups:
            qualified_table = fully_qualified_table(
                mysql_db_name_hack(kc_target, dest_conn)
            )
            dest_query = "SELECT {} FROM {}".format(
                columns_joined(target_cols), qualified_table
            )
            group_temps[(kc_target, target_cols)] = self.__stream_ids_to_source_temp(
                dest_query, target_cols, source_conn, dest_conn
            )

        fqt = fully_qualified_table(target)
        joins = ""
        for idx, kc in enumerate(relevant_key_constraints):
            key = (kc["target_table"], tuple(kc["target_columns"]))
            id_temp = group_temps[key]
            fk_cols = kc["fk_columns"]
            alias = "_ids{}".format(idx)
            join_conditions = " AND ".join(
                "{}.{} = {}.col{}::{}".format(
                    fqt, quoter(col), alias, i, fk_datatypes[col]
                )
                for i, col in enumerate(fk_cols)
            )
            joins += ' JOIN "{}" AS {} ON {}'.format(id_temp, alias, join_conditions)

        q = "SELECT {}.* FROM {}{}".format(fqt, fqt, joins)
        if upstream_filters:
            q += " WHERE {}".format(" AND ".join(upstream_filters))
        if self.config.max_rows_per_table is not None:
            q += " LIMIT {}".format(self.config.max_rows_per_table)
        self.__copy_rows(
            source_conn,
            dest_conn,
            q,
            target,
            batch_size=compute_batch_size(len(fk_datatypes)),
        )

    def __build_upstream_unnest_query(
        self, fqt, groups, fk_datatypes, upstream_filters, group_rows_map
    ):
        joins = ""
        all_params = []
        join_idx = 0
        for group_key, kcs in groups.items():
            rows = group_rows_map[group_key]
            for kc in kcs:
                fk_cols = kc["fk_columns"]
                unnest_args = ", ".join(
                    "%s::{}[]".format(fk_datatypes[col]) for col in fk_cols
                )
                join_cols = ", ".join("col{}".format(i) for i in range(len(fk_cols)))
                join_conditions = " AND ".join(
                    "{}.{} = ids{}.col{}".format(fqt, quoter(col), join_idx, i)
                    for i, col in enumerate(fk_cols)
                )
                joins += (
                    " JOIN unnest({unnest}) AS ids{idx}({join_cols}) ON {conds}".format(
                        unnest=unnest_args,
                        idx=join_idx,
                        join_cols=join_cols,
                        conds=join_conditions,
                    )
                )
                all_params.extend([row[i] for row in rows] for i in range(len(fk_cols)))
                join_idx += 1

        q = "SELECT {}.* FROM {}{}".format(fqt, fqt, joins)
        if upstream_filters:
            q += " WHERE {}".format(" AND ".join(upstream_filters))
        return q, all_params

    def __subset_upstream_unnest(
        self,
        target,
        relevant_key_constraints,
        upstream_filters,
        source_conn,
        dest_conn,
    ):
        fk_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(target), schema_name(target), source_conn
            )
        }

        groups = {}
        for kc in relevant_key_constraints:
            key = (kc["target_table"], tuple(kc["target_columns"]))
            groups.setdefault(key, []).append(kc)

        fqt = fully_qualified_table(target)
        batch_size = compute_batch_size(len(fk_datatypes))

        if len(groups) == 1 and self.config.max_rows_per_table is None:
            self.__upstream_unnest_streamed(
                target,
                fqt,
                groups,
                fk_datatypes,
                upstream_filters,
                batch_size,
                source_conn,
                dest_conn,
            )
            return

        self.__upstream_unnest_multi_group(
            target,
            fqt,
            groups,
            fk_datatypes,
            upstream_filters,
            batch_size,
            source_conn,
            dest_conn,
        )

    def __upstream_unnest_streamed(
        self,
        target,
        fqt,
        groups,
        fk_datatypes,
        upstream_filters,
        batch_size,
        source_conn,
        dest_conn,
    ):
        group_key = next(iter(groups))
        kc_target, target_cols = group_key

        qualified_table = fully_qualified_table(
            mysql_db_name_hack(kc_target, dest_conn)
        )
        query = "SELECT DISTINCT {} FROM {}".format(
            columns_joined(target_cols), qualified_table
        )

        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        dest_cursor = dest_conn.cursor(name=cursor_name, withhold=True)
        try:
            dest_cursor.execute(query)
            while True:
                batch = dest_cursor.fetchmany(batch_size)
                if not batch:
                    break
                valid_rows = [row for row in batch if all(c is not None for c in row)]
                if not valid_rows:
                    continue

                q, params = self.__build_upstream_unnest_query(
                    fqt,
                    groups,
                    fk_datatypes,
                    upstream_filters,
                    {group_key: valid_rows},
                )
                self.__copy_rows(
                    source_conn,
                    dest_conn,
                    q,
                    target,
                    params,
                    batch_size=compute_batch_size(len(fk_datatypes)),
                )
        finally:
            dest_cursor.close()

    def __upstream_unnest_multi_group(
        self,
        target,
        fqt,
        groups,
        fk_datatypes,
        upstream_filters,
        batch_size,
        source_conn,
        dest_conn,
    ):
        group_rows = {}
        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        dest_cursor = dest_conn.cursor(name=cursor_name, withhold=True)
        try:
            for kc_target, target_cols in groups:
                qualified_table = fully_qualified_table(
                    mysql_db_name_hack(kc_target, dest_conn)
                )
                query = "SELECT DISTINCT {} FROM {}".format(
                    columns_joined(target_cols), qualified_table
                )
                dest_cursor.execute(query)
                rows = []
                while True:
                    batch = dest_cursor.fetchmany(batch_size)
                    if not batch:
                        break
                    rows.extend(row for row in batch if all(c is not None for c in row))
                group_rows[(kc_target, target_cols)] = rows
        finally:
            dest_cursor.close()

        if not any(group_rows.values()):
            return

        largest_key = max(group_rows, key=lambda k: len(group_rows[k]))
        largest_rows = group_rows[largest_key]

        copy_batch = compute_batch_size(len(fk_datatypes))
        if len(largest_rows) <= batch_size:
            q, params = self.__build_upstream_unnest_query(
                fqt, groups, fk_datatypes, upstream_filters, group_rows
            )
            if self.config.max_rows_per_table is not None:
                q += " LIMIT {}".format(self.config.max_rows_per_table)
            self.__copy_rows(
                source_conn, dest_conn, q, target, params, batch_size=copy_batch
            )
            return

        for i in range(0, len(largest_rows), batch_size):
            batch_rows = largest_rows[i : i + batch_size]
            batch_map = dict(group_rows)
            batch_map[largest_key] = batch_rows
            q, params = self.__build_upstream_unnest_query(
                fqt, groups, fk_datatypes, upstream_filters, batch_map
            )
            self.__copy_rows(
                source_conn, dest_conn, q, target, params, batch_size=copy_batch
            )

    def subset_downstream(self, table, relationships, source_conn=None, dest_conn=None):
        source_conn = source_conn or self.__source_conn
        dest_conn = dest_conn or self.__destination_conn
        referencing_tables = self.__db_helper.get_redacted_table_references(
            table, self.__all_tables, source_conn
        )

        if len(referencing_tables) > 0:
            pk_columns = referencing_tables[0]["target_columns"]
        else:
            return

        temp_table = self.__db_helper.create_id_temp_table(dest_conn, len(pk_columns))

        for r in referencing_tables:
            fk_table = r["fk_table"]
            fk_columns = r["fk_columns"]

            select_q = (
                "SELECT DISTINCT {} FROM {} WHERE {} NOT IN (SELECT {} FROM {})".format(
                    columns_joined(fk_columns),
                    fully_qualified_table(mysql_db_name_hack(fk_table, dest_conn)),
                    columns_tupled(fk_columns),
                    columns_joined(pk_columns),
                    fully_qualified_table(mysql_db_name_hack(table, dest_conn)),
                )
            )
            insert_q = 'INSERT INTO "{}" {}'.format(temp_table, select_q)
            with dest_conn.cursor() as cur:
                cur.execute(insert_q)
            dest_conn.commit()

        columns_query = columns_to_copy(table, relationships, source_conn)

        if self.config.use_temp_tables:
            self.__subset_downstream_temp_tables(
                table, temp_table, pk_columns, columns_query, source_conn, dest_conn
            )
        else:
            self.__subset_downstream_unnest(
                table, temp_table, pk_columns, columns_query, source_conn, dest_conn
            )

    def __subset_downstream_temp_tables(
        self, table, dest_temp_table, pk_columns, columns_query, source_conn, dest_conn
    ):
        downstream_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(table), schema_name(table), source_conn
            )
        }
        dest_query = "SELECT DISTINCT * FROM {}".format(
            fully_qualified_table(dest_temp_table)
        )
        src_id_temp = self.__stream_ids_to_source_temp(
            dest_query, pk_columns, source_conn, dest_conn
        )
        q = self.__build_temp_table_join(
            table, src_id_temp, pk_columns, downstream_datatypes, columns_query
        )
        self.__copy_rows(
            source_conn,
            dest_conn,
            q,
            mysql_db_name_hack(table, dest_conn),
            batch_size=compute_batch_size(len(downstream_datatypes)),
        )

    def __subset_downstream_unnest(
        self, table, dest_temp_table, pk_columns, columns_query, source_conn, dest_conn
    ):
        downstream_datatypes = {
            col: typ
            for col, typ, _, _ in self.__db_helper.get_table_datatypes(
                table_name(table), schema_name(table), source_conn
            )
        }

        cursor_name = "table_cursor_" + str(uuid.uuid4()).replace("-", "")
        cursor = dest_conn.cursor(name=cursor_name, withhold=True)
        try:
            cursor_query = "SELECT DISTINCT * FROM {}".format(
                fully_qualified_table(dest_temp_table)
            )
            cursor.execute(cursor_query)
            batch_size = compute_batch_size(len(pk_columns))
            while True:
                rows = cursor.fetchmany(batch_size)
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
                self.__copy_rows(
                    source_conn,
                    dest_conn,
                    q,
                    mysql_db_name_hack(table, dest_conn),
                    params,
                    batch_size=compute_batch_size(len(downstream_datatypes)),
                )
        finally:
            cursor.close()

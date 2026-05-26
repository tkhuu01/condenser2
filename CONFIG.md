# Config

Configuration must exist in `config.json`. There is a minimal example in
`config.json.example` and a comprehensive example with all options in
`config.json.example_all`. Most of the configuration is straightforward:
source and destination DB connection details and subsetting settings.
There are three fields that deserve some additional attention.

The first is `initial_targets`. This is where you tell the subsetter to begin
the subset. You can specify any number of tables as an initial target, and
provide either a percent goal (e.g. 5% of the `users` table) or a WHERE clause.

Next is `dependency_breaks`. The subsetting tool cannot operate on databases
with cycles in their foreign key relationships. (Example: Table `events`
references `users`, which references `company`, which references `events` — a
cycle exists if you think about the foreign keys as a directed graph.) If your
database has a foreign key cycle (and many do), this field lets you tell the
subsetter to ignore certain foreign keys, essentially removing the cycle.
You'll have to know a bit about your database to use this field effectively.
The tool will warn you if you have a cycle that you haven't broken.

The last is `fk_augmentation`. Databases frequently have foreign keys that are
not codified as constraints on the database — these are implicit foreign keys.
For a subsetter to create useful subsets it needs to know about these implicit
constraints. This field lets you add foreign keys to the subsetter that the DB
doesn't have listed as a constraint.

Below we describe all configuration parameters. See `config.json.example_all`
for the exact format.

## Required

`db_type`: The type of the database to subset. Valid values are `"postgres"` or `"mysql"`.

`source_db_connection_info`: Source database connection details. A JSON object
with the fields `user_name`, `host`, `db_name`, `port`, `ssl_mode` (optional),
and `password` (optional). If `password` is omitted, you will be prompted for
a password. Any string field can reference an environment variable using
`${VAR_NAME}` syntax (e.g., `"password": "${DB_SOURCE_PASSWORD}"`). Values
without `${...}` are used as-is.

`destination_db_connection_info`: Destination database connection details. Same
fields and environment variable support as `source_db_connection_info`. If you
do not pass the `-y` flag, a confirmation prompt will appear unless the
destination is localhost or 127.0.0.1.

`initial_targets`: JSON array of JSON objects. Each object must contain a
`table` field (the target table) and exactly one of `where` or `percent`.
The `where` field specifies a WHERE clause for row selection. The `percent`
field indicates a percentage of the target table; it is equivalent to
`"where": "random() < <percent>/100.0"`.

## Table selection

`passthrough_tables`: Tables that will be copied to the destination database in
whole. The value is a JSON array of strings, of the form `"<schema>.<table>"`
for Postgres and `"<database>.<table>"` for MySQL.

`excluded_tables`: Tables that will be excluded from the subset. The table will
exist in the output, but contain no rows. The value is a JSON array of strings,
of the form `"<schema>.<table>"` for Postgres and `"<database>.<table>"` for
MySQL.

`keep_disconnected_tables`: If `true`, tables that the subset target(s) don't
reach when following foreign keys will be copied 100% over. If `false`
(default), their schema will be copied but the table contents will be empty.
The tables and foreign keys form a graph (tables are nodes, foreign keys are
directed edges); disconnected tables are those in components that don't contain
any targets.

`max_rows_per_table`: A limit applied to all tables being copied. Useful if you
have very large tables that you want a sampling from. Set to `"ALL"` for
unlimited (recommended for most use cases). Default is no limit.

## Foreign key configuration

`dependency_breaks`: An array of JSON objects with `fk_table` and
`target_table` fields specifying table relationships to ignore in order to
break cycles. Optionally include `preserve_fk_opportunistically: true` to
still preserve the foreign key relationship where possible without creating
cycles.

`fk_augmentation`: Additional foreign keys that, while not represented as
constraints in the database, are logically present in the data. Foreign keys
listed here are unioned with the foreign keys discovered from database
constraints. Each entry is a JSON object with `fk_table`, `fk_columns`,
`target_table`, and `target_columns`. The column arrays must be the same
length.

## Filtering

`upstream_filters`: Additional filtering applied to tables during upstream
subsetting. Upstream subsetting happens when a row is imported and the
subsetter greedily grabs rows from other tables that reference it via foreign
keys. If you don't want such greedy behavior on certain tables, you can impose
additional filters. Each entry is a JSON object with a `condition` field and
exactly one of `table` (filter applies to a specific table) or `column`
(filter applies to any table with that column). This is an advanced feature.

## Performance

`use_temp_tables`: If `true`, temporary ID tables will be created in the source
database so that IDs are not stored in Python memory when batching 100k rows.
This enables server-side JOINs, making subsetting more memory-efficient.
Requires write access on the source database (for `CREATE TEMPORARY TABLE`).
Default is `false`.

`use_copy_protocol`: If `true`, uses PostgreSQL's `COPY ... FROM STDIN`
protocol for row transfer instead of per-row INSERT statements. Significantly
faster (5-10x for bulk inserts). Postgres only. Default is `false`.

`parallel_read_workers`: Number of parallel connections used to read direct
target tables from the source. Splits work by physical page ranges (ctid),
so it works for any table regardless of primary key type. Designed for
read-only replicas. Requires PostgreSQL 12+. Default is `1` (sequential).

## Pre-filters

`pre_filters`: Named queries that execute once at the start of subsetting and
cache their results. Use this when an initial target needs to be filtered by a
slow or remote source (e.g., a foreign data wrapper table) that you don't want
re-executed per parallel worker. Each entry is a JSON object with `name` (a
reference key), `query` (the SQL to execute on the source), and `column` (the
target table column to filter against).

Initial targets reference a pre-filter by name via the optional `pre_filter`
field. The cached results are applied as `AND <column> = ANY(<cached values>)`
to the target's query.

## Incremental subsetting

`skip_schema_setup`: If `true`, the tool will not drop/recreate the destination
schema or run `pg_dump`. Use this when re-running the subsetter against an
existing destination database — for example, to add rows from different initial
targets across multiple runs. Duplicate rows are silently skipped via
`ON CONFLICT DO NOTHING`. Default is `false`.

## Post-processing

`pre_constraint_sql`: An array of SQL commands issued on the destination
database after subsetting is complete, but before database constraints have
been applied. Useful to clean up data that would otherwise violate constraints.
Prefer `post_subset_sql` for general-purpose queries.

`post_subset_sql`: An array of SQL commands issued on the destination database
after subsetting is complete and after database constraints have been applied.
Useful for additional ad-hoc tasks after subsetting.

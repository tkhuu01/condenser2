# db-condenser

Config-driven database subsetting tool for PostgreSQL and MySQL. Creates representative samples of databases while preserving referential integrity through foreign key graph traversal.

## Quick Reference

- **Language:** Python 3.10+
- **Package manager:** uv (Astral)
- **Entry point:** `src/db_condenser/direct_subset.py` → CLI command `subset`
- **Config:** `config.json` (see `config.json.example_all` for all options)

## Commands

```bash
# Install dependencies
uv sync --frozen

# Run the subsetter
uv run subset            # uses config.json
uv run subset -v         # verbose mode (query timing)
uv run subset --no-constraints  # skip FK constraint restoration

# Lint & format
uv run ruff format --check .
uv run ruff check --select I .   # import ordering
uv run ruff check .

# Fix lint issues
uv run ruff format .
uv run ruff check --fix .

# Run tests (requires PostgreSQL running on localhost:5432, user/pass: test/test)
uv run pytest tests/ -v
```

## Architecture

### Data Flow
1. `direct_subset.py` — CLI entry, parses args, orchestrates pipeline
2. `config_reader.py` — Loads config.json into dataclasses with validation
3. `psql_database_creator.py` / `mysql_database_creator.py` — Schema copy via pg_dump/mysqldump
4. `subset.py` — Core "middle-out" algorithm:
   - Direct targeting (initial_targets with WHERE or percent sampling)
   - Greedy upstream subsetting (tables with FKs to already-imported rows)
   - Pass-through tables (full copy, concurrent with 4 threads)
   - Downstream subsetting (referenced rows needed for FK integrity)
   - Disconnected tables (full copy or empty schema)
5. `result_tabulator.py` — Summary report of row counts

### Key Modules
- `subset_utils.py` — Query building, graph analysis (UnionFind, topo helpers), column selection
- `database_helper.py` — Factory that returns DB-specific helper module
- `db_connect.py` — Connection wrappers (PsqlConnection/MySqlConnection) with LoggingCursor
- `data_masking.py` — PII masking functions (null_out, mask_numbers, mask_characters, mask_email)
- `topo_orderer.py` — Topological sorting for table dependency order

### Upstream/Downstream Strategies

The upstream and downstream subsetting steps each have two code paths, selected by the `use_temp_tables` config flag:

- **`use_temp_tables: false` (default)** — Uses PostgreSQL `unnest()` to pass ID arrays as query parameters. IDs are held in Python memory in 100k-row batches. Only requires read access on the source DB.
- **`use_temp_tables: true`** — Streams IDs from the destination into temporary tables on the source DB, then does a server-side JOIN. Near-zero Python memory for ID lookups. Requires write access on the source (for `CREATE TEMPORARY TABLE`). Temp table columns are `varchar`, so the JOIN casts them to the source column's real datatype (e.g. `col0::int4`).

Shared helpers in `subset.py`:
- `__stream_ids_to_source_temp(dest_query, columns)` — streams ID rows into a source temp table
- `__build_temp_table_join(source_table, id_temp, columns, datatypes, select_expr)` — builds a `SELECT ... JOIN` with type casts

### Row Transfer Strategy

Row transfer from source to destination uses `copy_rows` in `psql_database_helper.py`, selected by the `use_copy_protocol` config flag:

- **`use_copy_protocol: false` (default)** — `copy_rows` uses `executemany` with per-row INSERT statements. Handles JSON columns via `psycopg.Json` wrapping and identity columns via `OVERRIDING SYSTEM VALUE`.
- **`use_copy_protocol: true`** — `copy_rows_copy_protocol` uses PostgreSQL's `COPY ... FROM STDIN` protocol with `write_row`. Significantly faster (5-10x for bulk inserts). JSON values pass through as text strings (no wrapping needed thanks to `set_json_loads(lambda s: s)`). COPY writes to identity columns natively (no override clause needed). Generated columns are excluded via the `COPY table(col_list)` column list.

The copy function is selected once in `Subset.__init__` and stored as `self.__copy_rows`, used by all 8 call sites.

### Database Support
- **PostgreSQL:** Full support including sequence reset, named cursors, JSON casting
- **MySQL:** Functional but limited (no sequence reset, no constraint re-application, no cross-db FKs)

## Code Style

- Formatter/linter: Ruff (format + isort + lint)
- No type stubs; uses standard type hints
- Dataclasses for config objects with `__post_init__` validation
- Global singleton `config` via `initialize()` / `get_config()`
- Factory pattern for DB-specific implementations
- Batch sizes: 100k rows (PostgreSQL upstream), 1k rows (MySQL)

## Testing

- Integration tests in `tests/psql/test_integration.py`
- Requires PostgreSQL 18 service (see docker-compose.yml or CI config)
- Test DB seeded from `tests/psql/seed.sql`, config in `tests/psql/test_config.json`
- Fixture `subsetter_dbs` is parameterized: every test runs three times (`[unnest]`, `[temp_tables]`, and `[copy_protocol]`)
- Each variant gets its own databases (e.g. `condenser_test_source`, `condenser_test_source_temp`, `condenser_test_source_copy`)
- CI runs on GitHub Actions: ruff lint + pytest with Postgres 18 service container

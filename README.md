# Database Condenser

A config-driven database subsetting tool for PostgreSQL and MySQL, forked from
Tonic's Condenser.

Some changes from the original Condenser:

* Concurrent thread pool usage to speed up subsetting
* Optional temp-table strategy for memory-efficent ID batching
* Optional Postgres COPY protocol for faster transfer
* Automatic sequence reset after subsetting
* Built on psycopg3 and managed with astral's uv

Subsetting data is the process of taking a representative sample of your data
in a manner that preserves the integrity of your database, e.g., give me 5% of
my users. If you do this naively, e.g., just grab 5% of all the tables in your
database, most likely, your database will break foreign key constraints. At
best, you’ll end up with a statistically non-representative data sample.

One common use-case is to scale down a production database to a more reasonable
size so that it can be used in staging, test, and development environments. This
can be done to save costs and, when used in tandem with PII removal, can be
quite powerful as a productivity enhancer. Another example is copying specific
rows from one database and placing them into another while maintaining referential
integrity.

You can find more about the original Condenser details
[here](https://www.tonic.ai/blog/condenser-a-database-subsetting-tool) and
[here](https://www.tonic.ai/blog/condenser-v2/).

## Installation

Six steps to set up, assuming Python 3.10+:

1. Install [astral-uv](https://docs.astral.sh/uv/getting-started/installation/)

2. Install Postgres and/or MySQL database tools. For Postgres we need `pg_dump`
and `psql` tools; they need to be on your `$PATH` or point to them with
`$POSTGRES_PATH`. For MySQL we need `mysqldump` and `mysql`, they can be on your
`$PATH` or point to them with `$MYSQL_PATH`.

3. Clone this project locally.

4. Install the project with `uv sync --frozen`

5. Setup your configuration and save it in `config.json`. The provided
`config.json.example` has the skeleton of what you need to provide: source and
destination database connection details, as well as subsetting goals in
`initial_targets`. Here's an example that will collect 10% of a table
named `public.target_table`.

    ```
    "initial_targets": [
        {
            "table": "public.target_table",
            "percent": 10
        }
    ]
    ```

    There may be more required configuration depending on your database, but
    simple databases should be easy. See the CONFIG.md for more details,
    and `config.json.example_all` for all of the options in a single config file.

6. Run! `$ uv run subset`


## Running

Almost all the configuration is in the `config.json` file, so running it is as simple as

```
$ uv run subset
```

Two command-line arguments are supported:

`-v`: Verbose output. Useful for performance debugging. Lists almost every
query made, and its speed.

`--no-constraints`: For Postgres this will not add constraints found in the source
database to the destination database. This option has no effect for MySQL.

`-y`: Skip confirmation on subsetting to destination host if it isn't localhost
or 127.0.0.1

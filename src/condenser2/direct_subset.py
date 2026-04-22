import argparse
import sys
import time

from condenser2 import config_reader, database_helper, result_tabulator
from condenser2.db_connect import DbConnect, MySqlConnection, PsqlConnection
from condenser2.mysql_database_creator import MySqlDatabaseCreator
from condenser2.psql_database_creator import PsqlDatabaseCreator
from condenser2.subset import Subset
from condenser2.subset_utils import print_progress


def db_creator(
    db_type: str, source: DbConnect, dest: DbConnect
) -> PsqlDatabaseCreator | MySqlDatabaseCreator:
    if db_type == "postgres":
        return PsqlDatabaseCreator(source, dest, False)
    elif db_type == "mysql":
        return MySqlDatabaseCreator(source, dest)
    else:
        raise ValueError("unknown db_type " + db_type)


def _parse_args():
    parser = argparse.ArgumentParser(description="Condenser2 database subsetter")
    parser.add_argument(
        "--stdin", action="store_true", help="Read config from stdin"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="Skip destination confirmation prompt"
    )
    parser.add_argument(
        "--no-constraints", action="store_true", help="Skip adding constraints"
    )
    return parser.parse_args()


def _confirm_destination(dest_info):
    print(
        f"\nDestination: {dest_info.host}:{dest_info.port}/{dest_info.db_name}"
        f" (user: {dest_info.user_name})"
    )
    response = input("Proceed with subsetting into this destination? [y/N] ")
    if response.lower() not in ("y", "yes"):
        print("Aborted.")
        sys.exit(1)


def main():
    args = _parse_args()

    if args.stdin:
        config_reader.initialize(sys.stdin)
    else:
        config_reader.initialize()

    db_type = config_reader.get_db_type()
    source_dbc = DbConnect(db_type, config_reader.get_source_db_connection_info())

    dest_info = config_reader.get_destination_db_connection_info()
    if not args.yes:
        _confirm_destination(dest_info)

    destination_dbc = DbConnect(db_type, dest_info)

    database = db_creator(db_type, source_dbc, destination_dbc)
    database.teardown()
    database.create()

    # Get list of tables to operate on
    db_helper = database_helper.get_specific_helper()
    all_tables = db_helper.list_all_tables(source_dbc)
    all_tables = [x for x in all_tables if x not in config_reader.get_excluded_tables()]

    subsetter = Subset(source_dbc, destination_dbc, all_tables)

    try:
        subsetter.prep_temp_dbs()
        subsetter.run_middle_out()

        print("Beginning pre constraint SQL calls")
        start_time = time.time()
        for idx, sql in enumerate(config_reader.get_pre_constraint_sql()):
            print_progress(sql, idx + 1, len(config_reader.get_pre_constraint_sql()))
            db_helper.run_query(sql, destination_dbc.get_db_connection())
        print(
            "Completed pre constraint SQL calls in {}s".format(time.time() - start_time)
        )

        print("Adding database constraints")
        if not args.no_constraints:
            database.add_constraints()

        print("Beginning post subset SQL calls")
        start_time = time.time()
        for idx, sql in enumerate(config_reader.get_post_subset_sql()):
            print_progress(sql, idx + 1, len(config_reader.get_post_subset_sql()))
            db_helper.run_query(sql, destination_dbc.get_db_connection())
        print("Completed post subset SQL calls in {}s".format(time.time() - start_time))

        print("Resetting sequence numbering")
        all_tables_no_pg = [table for table in all_tables if "pgbench" not in table]
        dest_conn = destination_dbc.get_db_connection()
        if db_type == "postgres":
            assert isinstance(dest_conn, PsqlConnection)
            db_helper.update_sequence_numbering(dest_conn, all_tables_no_pg)
        elif db_type == "mysql":
            # TODO update sequencing for mysql
            assert isinstance(dest_conn, MySqlConnection)
            # db_helper.update_sequence_numbering(
            #    dest_conn, all_tables_no_pg
            # )

        result_tabulator.tabulate(source_dbc, destination_dbc, all_tables)
    except KeyboardInterrupt:
        print("\nInterrupted — closing connections...")
        raise
    finally:
        subsetter.unprep_temp_dbs()
        subsetter.close_connections()


if __name__ == "__main__":
    main()

from condenser2 import config_reader


def get_specific_helper():
    if config_reader.get_db_type() == "postgres":
        from condenser2 import psql_database_helper

        return psql_database_helper
    else:
        from condenser2 import mysql_database_helper

        return mysql_database_helper

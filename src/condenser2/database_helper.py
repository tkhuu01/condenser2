from condenser2.config_reader import DbType, get_config


def get_specific_helper():
    config = get_config()
    if config.db_type == DbType.POSTGRES:
        from condenser2 import psql_database_helper

        return psql_database_helper
    else:
        from condenser2 import mysql_database_helper

        return mysql_database_helper

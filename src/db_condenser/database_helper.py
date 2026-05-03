from db_condenser.config_reader import DbType, get_config


def get_specific_helper():
    config = get_config()
    if config.db_type == DbType.POSTGRES:
        from db_condenser import psql_database_helper

        return psql_database_helper
    else:
        from db_condenser import mysql_database_helper

        return mysql_database_helper

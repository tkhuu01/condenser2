from db_condenser.backends.contracts import Backend
from db_condenser.config_reader import DbType


def get_backend(db_type: DbType) -> Backend:
    if db_type == DbType.POSTGRES:
        from db_condenser.backends.postgres import PostgresBackend

        return PostgresBackend()
    if db_type == DbType.MYSQL:
        from db_condenser.backends.mysql import MySqlBackend

        return MySqlBackend()
    raise ValueError("unknown db_type " + str(db_type))

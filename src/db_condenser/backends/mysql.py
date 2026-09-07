from db_condenser import mysql_database_helper
from db_condenser.backends.contracts import (
    BackendCapabilities,
    ConnectionFactory,
    SchemaManager,
)
from db_condenser.backends.legacy import LegacyOperations
from db_condenser.backends.session import BaseRunSession


class MySqlBackend(LegacyOperations):
    capabilities = BackendCapabilities(
        read_only_source=False,
        relationship_selection=False,
        incremental=False,
        shared_snapshot=False,
        parallel_reads=False,
        sequence_reset=False,
    )

    def __init__(self):
        super().__init__(mysql_database_helper)

    def open_run(self, source, destination, config):
        return MySqlRunSession(self, source, destination, config)

    def schema_manager(
        self, source: ConnectionFactory, destination: ConnectionFactory
    ) -> SchemaManager:
        from db_condenser.mysql_database_creator import MySqlDatabaseCreator

        return MySqlDatabaseCreator(source, destination)


class MySqlRunSession(BaseRunSession):
    def _initialize(self):
        if self.config.use_temp_tables:
            with self.source.cursor() as cur:
                cur.execute("SELECT @@global.read_only")
                (read_only,) = cur.fetchone()
            if read_only:
                raise RuntimeError(
                    "use_temp_tables is enabled but the source database is"
                    " read-only (@@global.read_only = 1)"
                )

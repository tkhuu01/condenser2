from db_condenser import mysql_database_helper
from db_condenser.backends.contracts import (
    BackendCapabilities,
    ConnectionFactory,
    SchemaManager,
)
from db_condenser.backends.legacy import LegacyOperations


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

    def schema_manager(
        self, source: ConnectionFactory, destination: ConnectionFactory
    ) -> SchemaManager:
        from db_condenser.mysql_database_creator import MySqlDatabaseCreator

        return MySqlDatabaseCreator(source, destination)

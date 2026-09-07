from db_condenser import psql_database_helper
from db_condenser.backends.contracts import (
    BackendCapabilities,
    ConnectionFactory,
    SchemaManager,
)
from db_condenser.backends.legacy import LegacyOperations


class PostgresBackend(LegacyOperations):
    capabilities = BackendCapabilities(
        read_only_source=True,
        relationship_selection=True,
        incremental=True,
        shared_snapshot=True,
        parallel_reads=True,
        sequence_reset=True,
    )

    def __init__(self):
        super().__init__(psql_database_helper)

    def schema_manager(
        self, source: ConnectionFactory, destination: ConnectionFactory
    ) -> SchemaManager:
        from db_condenser.psql_database_creator import PsqlDatabaseCreator

        return PsqlDatabaseCreator(source, destination, False)

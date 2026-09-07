"""Connection ownership and scratch lifecycle for one subset run."""

from db_condenser.backends.contracts import Backend, ConnectionFactory
from db_condenser.config_reader import Config


class BaseRunSession:
    def __init__(
        self,
        backend: Backend,
        source: ConnectionFactory,
        destination: ConnectionFactory,
        config: Config,
    ):
        self.backend = backend
        self.config = config
        self._source_factory = source
        self._connections = []
        self.source_pool = []
        try:
            self.source = source.get_db_connection(read_repeatable=True)
            self._connections.append(self.source)
            self.destination = destination.get_db_connection()
            self._connections.append(self.destination)
            backend.turn_off_constraints(self.destination)
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        pass

    def open_source_connection(self):
        """Return a worker connection; the caller owns and closes it."""
        return self._source_factory.get_db_connection(read_repeatable=True)

    def prepare(self):
        self.backend.prep_temp_dbs(self.source, self.destination)

    def prepare_incremental(self, tables: list[str]):
        raise NotImplementedError("This backend does not support incremental runs")

    def finish(self, succeeded: bool):
        self.backend.unprep_temp_dbs(self.source, self.destination)

    def close(self):
        # Attempt every close even if one driver raises; closing the exporting
        # connection ends the source snapshot and releases transaction locks.
        connections, self._connections = self._connections, []
        error = None
        for connection in connections:
            try:
                connection.close()
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

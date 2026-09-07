"""Keep integration artifacts temporary and honor explicit test endpoints."""

import os

import pytest

from db_condenser import config_reader


@pytest.fixture(autouse=True, scope="session")
def isolated_postgres_test(tmp_path_factory):
    original = config_reader._raw_dict_to_config

    def test_config(raw):
        if raw.get("db_type") != "postgres":
            return original(raw)
        raw = dict(raw)
        for field in ("source_db_connection_info", "destination_db_connection_info"):
            if field not in raw:
                continue
            connection = dict(raw[field])
            for key, variable in (
                ("host", "POSTGRES_HOST"),
                ("port", "POSTGRES_PORT"),
                ("user_name", "POSTGRES_USER"),
                ("password", "POSTGRES_PASSWORD"),
            ):
                if variable in os.environ:
                    connection[key] = os.environ[variable]
            raw[field] = connection
        return original(raw)

    # Existing integration fixtures are module-scoped, so endpoint overrides
    # must be installed before those fixtures create their databases.
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path_factory.mktemp("postgres"))
        patch.setattr(config_reader, "config", config_reader.config)
        patch.setattr(config_reader, "_raw_dict_to_config", test_config)
        yield

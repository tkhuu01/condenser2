"""Unit tests must not depend on a database or leak global configuration."""

import mysql.connector
import psycopg
import pytest

from db_condenser import config_reader


@pytest.fixture(autouse=True)
def isolated_unit_test(monkeypatch):
    monkeypatch.setattr(config_reader, "config", None)

    def unexpected_connection(*args, **kwargs):
        pytest.fail("Unit tests cannot open database connections")

    monkeypatch.setattr(psycopg, "connect", unexpected_connection)
    monkeypatch.setattr(mysql.connector, "connect", unexpected_connection)

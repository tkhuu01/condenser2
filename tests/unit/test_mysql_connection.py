"""Connection option contract, without opening a socket."""

from types import SimpleNamespace

import mysql.connector

from db_condenser.db_connect import MySqlConnection


def test_mysql_connection_forwards_configured_port(monkeypatch):
    calls = []

    def connect(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(mysql.connector, "connect", connect)
    info = SimpleNamespace(
        host="localhost", user="test", password="test", db_name="test", port=53316
    )
    MySqlConnection(info, read_repeatable=False)
    assert calls[0].get("port") == 53316

"""Characterize graph decisions independently of either SQL backend."""

from types import SimpleNamespace

import pytest
from toposort import CircularDependencyError

from db_condenser import config_reader
from db_condenser.config_reader import DependencyBreak
from db_condenser.subset_utils import (
    compute_disconnected_tables,
    compute_downstream_strata,
    compute_upstream_strata,
)
from db_condenser.topo_orderer import get_topological_order_by_tables


def relationship(child, parent):
    return {
        "fk_table": child,
        "fk_columns": ["parent_id"],
        "target_table": parent,
        "target_columns": ["id"],
    }


@pytest.fixture
def graph_config(monkeypatch):
    config = SimpleNamespace(dependency_breaks=[])
    monkeypatch.setattr(config_reader, "config", config)
    return config


def test_fk_direction_and_upstream_downstream_order(graph_config):
    relationships = [relationship("orders", "users"), relationship("users", "tenants")]
    order = get_topological_order_by_tables(
        relationships, ["orders", "users", "tenants"]
    )
    assert order == [{"tenants"}, {"users"}, {"orders"}]
    assert compute_upstream_strata(["users"], order) == [{"orders"}]
    assert compute_downstream_strata(["orders"], [], order) == [{"users"}, {"tenants"}]


def test_sibling_tables_share_a_stratum(graph_config):
    relationships = [relationship("orders", "users"), relationship("events", "users")]
    assert get_topological_order_by_tables(
        relationships, ["users", "orders", "events"]
    ) == [{"users"}, {"orders", "events"}]


def test_disconnected_tables_respect_direct_and_passthrough_roots():
    tables = ["users", "orders", "audit", "audit_details", "unrelated"]
    relationships = [
        relationship("orders", "users"),
        relationship("audit_details", "audit"),
    ]
    assert compute_disconnected_tables(["users"], ["audit"], tables, relationships) == [
        "unrelated"
    ]


def test_cycle_requires_an_explicit_dependency_break(graph_config):
    relationships = [relationship("a", "b"), relationship("b", "a")]
    with pytest.raises(CircularDependencyError):
        get_topological_order_by_tables(relationships, ["a", "b"])
    graph_config.dependency_breaks = [DependencyBreak(fk_table="a", target_table="b")]
    assert get_topological_order_by_tables(relationships, ["a", "b"]) == [{"a"}, {"b"}]


def test_self_reference_requires_an_explicit_dependency_break(graph_config):
    relationships = [relationship("a", "a")]
    with pytest.raises(ValueError, match="depends on itself"):
        get_topological_order_by_tables(relationships, ["a"])
    graph_config.dependency_breaks = [DependencyBreak(fk_table="a", target_table="a")]
    assert get_topological_order_by_tables(relationships, ["a"]) == []


def test_excluded_relations_do_not_enter_traversal(graph_config):
    relationships = [
        relationship("orders", "users"),
        relationship("unselected", "users"),
    ]
    assert get_topological_order_by_tables(relationships, ["orders", "users"]) == [
        {"users"},
        {"orders"},
    ]

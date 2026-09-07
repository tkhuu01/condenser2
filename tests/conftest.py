"""Test selection; regular test runs never enable performance workloads."""

from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-performance",
        action="store_true",
        help="Run synthetic performance baselines",
    )


def pytest_collection_modifyitems(config, items):
    root = Path(__file__).parent
    for item in items:
        group = item.path.relative_to(root).parts[0]
        if group == "unit":
            item.add_marker(pytest.mark.unit)
        elif group in ("psql", "mysql", "performance"):
            item.add_marker(pytest.mark.integration)
            item.add_marker(
                pytest.mark.mysql if group == "mysql" else pytest.mark.postgres
            )
        if group == "performance":
            item.add_marker(pytest.mark.performance)
            if not config.getoption("--run-performance"):
                item.add_marker(pytest.mark.skip(reason="requires --run-performance"))

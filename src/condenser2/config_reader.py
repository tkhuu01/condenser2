import collections
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


@dataclass
class InitialTarget:
    table: str
    percent: float | None = None
    where: str | None = None

    def __post_init__(self):
        # Exactly one of where/percent must be set
        if (self.where is None) == (self.percent is None):
            raise ValueError(
                "Initial Target must specify exactly one of 'where' or 'percent'"
            )


class DbType(str, Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"


@dataclass(frozen=True)
class DbConnectInfo:
    user_name: str
    host: str
    db_name: str
    ssl_mode: str | None = None
    # No password will prompt user
    password: str | None = None
    port: int = 5432


@dataclass
class UpstreamFilter:
    condition: str
    table: str | None = None
    column: str | None = None

    def __post_init__(self):
        # Exactly one of table/column must be set
        if (self.table is None) == (self.column is None):
            raise ValueError(
                "Upstream filters must specify exactly one of 'table' or 'column'"
            )


@dataclass
class DependencyBreak:
    fk_table: str
    target_table: str


@dataclass
class FkAugmentation:
    fk_table: str
    fk_columns: list[str]
    target_table: str
    target_columns: list[str]

    def __post_init__(self):
        if len(self.fk_columns) != len(self.target_columns):
            raise ValueError("fk_columns and target_columns must be the same length")


LOCAL_POSTGRES_HOST = DbConnectInfo(
    user_name="postgres",
    host="localhost",
    db_name="postgres",
    password="postgres",
    port=5432,
)


@dataclass
class Config:
    db_type: DbType
    initial_targets: list[InitialTarget]
    source_db_connection_info: DbConnectInfo
    keep_disconnected_tables: bool = False
    upstream_filters: list[UpstreamFilter] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
    passthrough_tables: list[str] = field(default_factory=list)
    dependency_breaks: list[DependencyBreak] = field(default_factory=list)
    fk_augmentation: list[FkAugmentation] = field(default_factory=list)
    max_rows_per_table: int | Literal["ALL"] | None = None
    pre_constraint_sql: list[str] = field(default_factory=list)
    post_subset_sql: list[str] = field(default_factory=list)
    destination_db_connection_info: DbConnectInfo = LOCAL_POSTGRES_HOST


_config: dict = {}


def _raw_dict_to_config(raw_config: dict) -> Config:
    initial_targets = []
    db_type = DbType(raw_config["db_type"].lower())
    initial_targets = [
        InitialTarget(**target) for target in raw_config["initial_targets"]
    ]
    source_db = DbConnectInfo(**raw_config["source_db_connection_info"])
    upstream_filters = [
        UpstreamFilter(**table) for table in raw_config.get("upstream_filters", [])
    ]
    excluded_tables = [table for table in raw_config.get("excluded_tables", [])]
    passthrough_tables = [table for table in raw_config.get("passthrough_tables", [])]
    dependency_breaks = [
        DependencyBreak(**relation)
        for relation in raw_config.get("dependency_breaks", [])
    ]
    fk_augmentation = [
        FkAugmentation(**fka) for fka in raw_config.get("fk_augmentation", [])
    ]
    pre_constraint_sql = [sql for sql in raw_config.get("pre_constraint_sql", [])]
    post_subset_sql = [sql for sql in raw_config.get("post_subset_sql", [])]
    return Config(
        db_type=db_type,
        initial_targets=initial_targets,
        source_db_connection_info=source_db,
        keep_disconnected_tables=bool(
            raw_config.get("keep_disconnected_tables", False)
        ),
        upstream_filters=upstream_filters,
        excluded_tables=excluded_tables,
        passthrough_tables=passthrough_tables,
        dependency_breaks=dependency_breaks,
        fk_augmentation=fk_augmentation,
        pre_constraint_sql=pre_constraint_sql,
        post_subset_sql=post_subset_sql,
    )


def initialize(file_like=None):
    global _config
    if _config:
        print("WARNING: Attempted to initialize configuration twice.", file=sys.stderr)

    if not file_like:
        with open("config.json", "r") as fp:
            _config = json.load(fp)
    else:
        _config = json.load(file_like)

    print(_raw_dict_to_config(_config))
    #raise Exception()


DependencyBreak_ = collections.namedtuple(
    "DependencyBreak", ["fk_table", "target_table"]
)


def get_dependency_breaks():
    return set(
        [
            DependencyBreak_(b["fk_table"], b["target_table"])
            for b in _config["dependency_breaks"]
        ]
    )


def get_preserve_fk_opportunistically():
    return set(
        [
            DependencyBreak_(b["fk_table"], b["target_table"])
            for b in _config["dependency_breaks"]
            if "perserve_fk_opportunistically" in b
            and b["perserve_fk_opportunistically"]
        ]
    )


def get_initial_targets():
    return _config["initial_targets"]


def get_initial_target_tables():
    return [target["table"] for target in _config["initial_targets"]]


def keep_disconnected_tables():
    return "keep_disconnected_tables" in _config and bool(
        _config["keep_disconnected_tables"]
    )


def get_db_type():
    return _config["db_type"]


def get_source_db_connection_info():
    return _config["source_db_connection_info"]


def get_destination_db_connection_info():
    destination = _config.get("destination_db_connection_info")
    return destination if destination else LOCAL_POSTGRES_HOST


def get_excluded_tables():
    return list(_config["excluded_tables"])


def get_passthrough_tables():
    return list(_config["passthrough_tables"])


def get_fk_augmentation():
    return list(map(__convert_tonic_format, _config["fk_augmentation"]))


def get_upstream_filters():
    return _config["upstream_filters"]


def get_pre_constraint_sql():
    return _config["pre_constraint_sql"] if "pre_constraint_sql" in _config else []


def get_post_subset_sql():
    return _config["post_subset_sql"] if "post_subset_sql" in _config else []


def get_max_rows_per_table():
    return _config["max_rows_per_table"] if "max_rows_per_table" in _config else None


def __convert_tonic_format(obj):
    if "fk_schema" in obj:
        return {
            "fk_table": obj["fk_schema"] + "." + obj["fk_table"],
            "fk_columns": obj["fk_columns"],
            "target_table": obj["target_schema"] + "." + obj["target_table"],
            "target_columns": obj["target_columns"],
        }
    else:
        return obj


def verbose_logging():
    return "-v" in sys.argv

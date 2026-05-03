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


@dataclass
class DbConnectInfo:
    user_name: str
    host: str
    db_name: str
    port: int
    ssl_mode: str | None = None
    # No password will prompt user
    password: str | None = None


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
    preserve_fk_opportunistically: bool = False


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

LOCAL_MYSQL_HOST = DbConnectInfo(
    user_name="root", host="localhost", db_name="default_db", password="", port=3306
)


@dataclass
class Config:
    db_type: DbType
    initial_targets: list[InitialTarget]
    source_db_connection_info: DbConnectInfo
    destination_db_connection_info: DbConnectInfo
    keep_disconnected_tables: bool = False
    upstream_filters: list[UpstreamFilter] = field(default_factory=list)
    excluded_tables: list[str] = field(default_factory=list)
    passthrough_tables: list[str] = field(default_factory=list)
    dependency_breaks: list[DependencyBreak] = field(default_factory=list)
    fk_augmentation: list[FkAugmentation] = field(default_factory=list)
    max_rows_per_table: int | Literal["ALL"] | None = None
    pre_constraint_sql: list[str] = field(default_factory=list)
    post_subset_sql: list[str] = field(default_factory=list)

    @property
    def dependency_break_set(self) -> set[tuple[str, str]]:
        return {(b.fk_table, b.target_table) for b in self.dependency_breaks}

    @property
    def preserve_fk_opportunistically(self) -> set[tuple[str, str]]:
        return {
            (b.fk_table, b.target_table)
            for b in self.dependency_breaks
            if b.preserve_fk_opportunistically
        }

    @property
    def initial_target_tables(self) -> list[str]:
        return [target.table for target in self.initial_targets]


config: Config | None = None


def _raw_dict_to_config(raw_config: dict) -> Config:
    initial_targets = []
    db_type = DbType(raw_config["db_type"].lower())

    initial_targets = [
        InitialTarget(**target) for target in raw_config["initial_targets"]
    ]
    default_localhost = (
        LOCAL_POSTGRES_HOST if db_type == DbType.POSTGRES else LOCAL_MYSQL_HOST
    )
    source_db = DbConnectInfo(**raw_config["source_db_connection_info"])
    dest_db = DbConnectInfo(
        **raw_config.get("destination_db_connection_info", default_localhost)
    )

    upstream_filters = [
        UpstreamFilter(**table) for table in raw_config.get("upstream_filters", [])
    ]

    excluded_tables = [table for table in raw_config.get("excluded_tables", [])]
    passthrough_tables = list(
        set([table for table in raw_config.get("passthrough_tables", [])])
    )
    dependency_breaks = [
        DependencyBreak(**relation)
        for relation in raw_config.get("dependency_breaks", [])
    ]
    fk_augmentation = []
    for fka in raw_config.get("fk_augmentation", []):
        if "fk_schema" in fka:
            fka = {
                "fk_table": fka["fk_schema"] + "." + fka["fk_table"],
                "fk_columns": fka["fk_columns"],
                "target_table": fka["target_schema"] + "." + fka["target_table"],
                "target_columns": fka["target_columns"],
            }
        fk_augmentation.append(FkAugmentation(**fka))

    pre_constraint_sql = [sql for sql in raw_config.get("pre_constraint_sql", [])]
    post_subset_sql = [sql for sql in raw_config.get("post_subset_sql", [])]
    max_rows_per_table = raw_config.get("max_rows_per_table", None)
    return Config(
        db_type=db_type,
        initial_targets=initial_targets,
        source_db_connection_info=source_db,
        destination_db_connection_info=dest_db,
        keep_disconnected_tables=bool(
            raw_config.get("keep_disconnected_tables", False)
        ),
        upstream_filters=upstream_filters,
        excluded_tables=excluded_tables,
        passthrough_tables=passthrough_tables,
        dependency_breaks=dependency_breaks,
        fk_augmentation=fk_augmentation,
        max_rows_per_table=max_rows_per_table,
        pre_constraint_sql=pre_constraint_sql,
        post_subset_sql=post_subset_sql,
    )


def initialize(file_like=None):
    global config
    if config:
        print("WARNING: Attempted to initialize configuration twice.", file=sys.stderr)

    if not file_like:
        with open("config.json", "r") as fp:
            raw_config = json.load(fp)
    else:
        raw_config = json.load(file_like)

    config = _raw_dict_to_config(raw_config)


def get_config() -> Config:
    if config is None:
        raise RuntimeError("Config not initialized — call initialize() first")
    return config


def reset_config():
    global config
    config = None

import duckdb

from contextlib import contextmanager
from typing import Any, AnyStr, ClassVar, Iterator, Optional, Sequence
from dlt.common.destination import DestinationCapabilitiesContext

from dlt.destinations.exceptions import (
    DatabaseTerminalException,
    DatabaseTransientException,
    DatabaseUndefinedRelation,
)
from dlt.destinations.typing import DBApi, DBApiCursor, DBTransaction, DataFrame
from dlt.destinations.sql_client import (
    SqlClientBase,
    DBApiCursorImpl,
    raise_database_error,
    raise_open_connection_error,
)

from dlt.destinations.impl.duckdb.sql_client import DuckDbSqlClient, DuckDBDBApiCursorImpl
from dlt.destinations.impl.motherduck import capabilities
from dlt.destinations.impl.motherduck.configuration import MotherDuckCredentials


class MotherDuckSqlClient(DuckDbSqlClient):
    capabilities: ClassVar[DestinationCapabilitiesContext] = capabilities()

    def __init__(self, dataset_name: str, credentials: MotherDuckCredentials) -> None:
        super().__init__(dataset_name, credentials)
        self.database_name = credentials.database

    def fully_qualified_dataset_name(self, escape: bool = True) -> str:
        dataset_name = super().fully_qualified_dataset_name(escape)
        database_name = self.capabilities.case_identifier(self.database_name)
        if escape:
            database_name = self.capabilities.escape_identifier(database_name)
        return f"{database_name}.{dataset_name}"

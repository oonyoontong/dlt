import posixpath
import pathlib
import os
import base64
import pyarrow as pa

from deltalake import write_deltalake
from types import TracebackType
from typing import ClassVar, List, Type, Iterable, Dict, Iterator, Optional, Tuple, cast, Callable
from fsspec import AbstractFileSystem
from contextlib import contextmanager

import dlt
from dlt.common import logger, time, json, pendulum
from dlt.common.typing import DictStrAny
from dlt.common.schema import Schema, TSchemaTables, TTableSchema
from dlt.common.schema.typing import TWriteDisposition
from dlt.common.storages import FileStorage, fsspec_from_config
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.destination.reference import (
    NewLoadJob,
    TLoadJobState,
    LoadJob,
    JobClientBase,
    FollowupJob,
    WithStagingDataset,
    WithStateSync,
    StorageSchemaInfo,
    StateInfo,
    DoNothingJob,
)
from dlt.common.destination.exceptions import DestinationUndefinedEntity
from dlt.destinations.job_impl import EmptyLoadJob
from dlt.destinations.impl.filesystem import capabilities
from dlt.destinations.impl.filesystem.configuration import FilesystemDestinationClientConfiguration
from dlt.destinations.job_impl import NewReferenceJob
from dlt.destinations import path_utils
from dlt.destinations.fs_client import FSClientBase

INIT_FILE_NAME = "init"
FILENAME_SEPARATOR = "__"


class LoadFilesystemJob(LoadJob):
    def __init__(
        self,
        client: "FilesystemClient",
        local_path: str,
        load_id: str,
        table: TTableSchema,
    ) -> None:
        self.client = client
        self.table = table
        self.is_local_filesystem = client.config.protocol == "file"
        # pick local filesystem pathlib or posix for buckets
        self.pathlib = os.path if self.is_local_filesystem else posixpath

        file_name = FileStorage.get_file_name_from_file_path(local_path)
        super().__init__(file_name)

        if table["table_format"] == "delta":
            import pyarrow.parquet as pq

            client._write_delta_table(
                path=self.make_remote_uri(),
                table=pq.read_table(local_path),
                write_disposition=table["write_disposition"],
            )
        else:
            self.destination_file_name = path_utils.create_path(
                client.config.layout,
                file_name,
                client.schema.name,
                load_id,
                current_datetime=client.config.current_datetime,
                load_package_timestamp=dlt.current.load_package()["state"]["created_at"],
                extra_placeholders=client.config.extra_placeholders,
            )
            # We would like to avoid failing for local filesystem where
            # deeply nested directory will not exist before writing a file.
            # It `auto_mkdir` is disabled by default in fsspec so we made some
            # trade offs between different options and decided on this.
            # remote_path = f"{client.config.protocol}://{posixpath.join(dataset_path, destination_file_name)}"
            remote_path = self.make_remote_path()
            if self.is_local_filesystem:
                client.fs_client.makedirs(self.pathlib.dirname(remote_path), exist_ok=True)
            client.fs_client.put_file(local_path, remote_path)

    def make_remote_path(self) -> str:
        """Returns path on the remote filesystem to which copy the file, without scheme. For local filesystem a native path is used"""
        if self.table["table_format"] == "delta":  # directory path
            return self.client.get_table_dir(self.table["name"])
        else:  # file path
            # path.join does not normalize separators and available
            # normalization functions are very invasive and may string the trailing separator
            return self.pathlib.join(  # type: ignore[no-any-return]
                self.client.dataset_path,
                path_utils.normalize_path_sep(self.pathlib, self.destination_file_name),
            )

    def make_remote_uri(self) -> str:
        """Returns uri to the remote filesystem to which copy the file"""
        remote_path = self.make_remote_path()
        if self.is_local_filesystem:
            return self.client.config.make_file_uri(remote_path)
        else:
            return f"{self.client.config.protocol}://{remote_path}"

    def state(self) -> TLoadJobState:
        return "completed"

    def exception(self) -> str:
        raise NotImplementedError()


class FollowupFilesystemJob(FollowupJob, LoadFilesystemJob):
    def create_followup_jobs(self, final_state: TLoadJobState) -> List[NewLoadJob]:
        jobs = super().create_followup_jobs(final_state)
        if final_state == "completed":
            ref_job = NewReferenceJob(
                file_name=self.file_name(), status="running", remote_path=self.make_remote_uri()
            )
            jobs.append(ref_job)
        return jobs


class FilesystemClient(FSClientBase, JobClientBase, WithStagingDataset, WithStateSync):
    """filesystem client storing jobs in memory"""

    capabilities: ClassVar[DestinationCapabilitiesContext] = capabilities()
    fs_client: AbstractFileSystem
    # a path (without the scheme) to a location in the bucket where dataset is present
    bucket_path: str
    # name of the dataset
    dataset_name: str

    def __init__(self, schema: Schema, config: FilesystemDestinationClientConfiguration) -> None:
        super().__init__(schema, config)
        self.fs_client, fs_path = fsspec_from_config(config)
        self.is_local_filesystem = config.protocol == "file"
        self.bucket_path = (
            config.make_local_path(config.bucket_url) if self.is_local_filesystem else fs_path
        )
        # pick local filesystem pathlib or posix for buckets
        self.pathlib = os.path if self.is_local_filesystem else posixpath

        self.config: FilesystemDestinationClientConfiguration = config
        # verify files layout. we need {table_name} and only allow {schema_name} before it, otherwise tables
        # cannot be replaced and we cannot initialize folders consistently
        self.table_prefix_layout = path_utils.get_table_prefix_layout(config.layout)
        self.dataset_name = self.config.normalize_dataset_name(self.schema)

    def drop_storage(self) -> None:
        if self.is_storage_initialized():
            self.fs_client.rm(self.dataset_path, recursive=True)

    @property
    def dataset_path(self) -> str:
        """A path within a bucket to tables in a dataset
        NOTE: dataset_name changes if with_staging_dataset is active
        """
        return self.pathlib.join(self.bucket_path, self.dataset_name)  # type: ignore[no-any-return]

    @contextmanager
    def with_staging_dataset(self) -> Iterator["FilesystemClient"]:
        current_dataset_name = self.dataset_name
        try:
            self.dataset_name = self.schema.naming.normalize_table_identifier(
                current_dataset_name + "_staging"
            )
            yield self
        finally:
            # restore previous dataset name
            self.dataset_name = current_dataset_name

    def initialize_storage(self, truncate_tables: Iterable[str] = None) -> None:
        # clean up existing files for tables selected for truncating
        if truncate_tables and self.fs_client.isdir(self.dataset_path):
            # get all dirs with table data to delete. the table data are guaranteed to be files in those folders
            # TODO: when we do partitioning it is no longer the case and we may remove folders below instead
            logger.info(f"Will truncate tables {truncate_tables}")
            self.truncate_tables(list(truncate_tables))

        # we mark the storage folder as initialized
        self.fs_client.makedirs(self.dataset_path, exist_ok=True)
        self.fs_client.touch(self.pathlib.join(self.dataset_path, INIT_FILE_NAME))

    def truncate_tables(self, table_names: List[str]) -> None:
        """Truncate table with given name"""
        table_dirs = set(self.get_table_dirs(table_names))
        table_prefixes = [self.get_table_prefix(t) for t in table_names]
        for table_dir in table_dirs:
            for table_file in self.list_files_with_prefixes(table_dir, table_prefixes):
                # NOTE: deleting in chunks on s3 does not raise on access denied, file non existing and probably other errors
                # print(f"DEL {table_file}")
                try:
                    # NOTE: must use rm_file to get errors on delete
                    self.fs_client.rm_file(table_file)
                except NotImplementedError:
                    # not all filesystem implement the above
                    self.fs_client.rm(table_file)
                    if self.fs_client.exists(table_file):
                        raise FileExistsError(table_file)
                except FileNotFoundError:
                    logger.info(
                        f"Directory or path to truncate tables {table_names} does not exist but"
                        " it should have been created previously!"
                    )

    def update_stored_schema(
        self,
        only_tables: Iterable[str] = None,
        expected_update: TSchemaTables = None,
    ) -> TSchemaTables:
        # create destination dirs for all tables
        table_names = only_tables or self.schema.tables.keys()
        dirs_to_create = self.get_table_dirs(table_names)
        for tables_name, directory in zip(table_names, dirs_to_create):
            self.fs_client.makedirs(directory, exist_ok=True)
            # we need to mark the folders of the data tables as initialized
            if tables_name in self.schema.dlt_table_names():
                self.fs_client.touch(self.pathlib.join(directory, INIT_FILE_NAME))

        # don't store schema when used as staging
        if not self.config.as_staging:
            self._store_current_schema()

        return expected_update

    def get_table_dir(self, table_name: str) -> str:
        # dlt tables do not respect layout (for now)
        table_prefix = self.get_table_prefix(table_name)
        return self.pathlib.dirname(table_prefix)  # type: ignore[no-any-return]

    def get_table_prefix(self, table_name: str) -> str:
        # dlt tables do not respect layout (for now)
        if table_name.startswith(self.schema._dlt_tables_prefix):
            # dlt tables get layout where each tables is a folder
            # it is crucial to append and keep "/" at the end
            table_prefix = self.pathlib.join(table_name, "")
        else:
            table_prefix = self.table_prefix_layout.format(
                schema_name=self.schema.name, table_name=table_name
            )
        return self.pathlib.join(  # type: ignore[no-any-return]
            self.dataset_path, path_utils.normalize_path_sep(self.pathlib, table_prefix)
        )

    def get_table_dirs(self, table_names: Iterable[str]) -> List[str]:
        """Gets directories where table data is stored."""
        return [self.get_table_dir(t) for t in table_names]

    def list_table_files(self, table_name: str) -> List[str]:
        """gets list of files associated with one table"""
        table_dir = self.get_table_dir(table_name)
        # we need the table prefix so we separate table files if in the same folder
        table_prefix = self.get_table_prefix(table_name)
        return self.list_files_with_prefixes(table_dir, [table_prefix])

    def list_files_with_prefixes(self, table_dir: str, prefixes: List[str]) -> List[str]:
        """returns all files in a directory that match given prefixes"""
        result = []
        for current_dir, _dirs, files in self.fs_client.walk(table_dir, detail=False, refresh=True):
            for file in files:
                # skip INIT files
                if file == INIT_FILE_NAME:
                    continue
                filepath = self.pathlib.join(
                    path_utils.normalize_path_sep(self.pathlib, current_dir), file
                )
                for p in prefixes:
                    if filepath.startswith(p):
                        result.append(filepath)
                        break
        return result

    def is_storage_initialized(self) -> bool:
        return self.fs_client.exists(self.pathlib.join(self.dataset_path, INIT_FILE_NAME))  # type: ignore[no-any-return]

    def start_file_load(self, table: TTableSchema, file_path: str, load_id: str) -> LoadJob:
        # skip the state table, we create a jsonl file in the complete_load step
        # this does not apply to scenarios where we are using filesystem as staging
        # where we want to load the state the regular way
        if table["name"] == self.schema.state_table_name and not self.config.as_staging:
            return DoNothingJob(file_path)

        cls = FollowupFilesystemJob if self.config.as_staging else LoadFilesystemJob
        return cls(self, file_path, load_id, table)

    def restore_file_load(self, file_path: str) -> LoadJob:
        return EmptyLoadJob.from_file_path(file_path, "completed")

    def __enter__(self) -> "FilesystemClient":
        return self

    def __exit__(
        self, exc_type: Type[BaseException], exc_val: BaseException, exc_tb: TracebackType
    ) -> None:
        pass

    def should_load_data_to_staging_dataset(self, table: TTableSchema) -> bool:
        return False

    #
    # state stuff
    #

    def _write_to_json_file(self, filepath: str, data: DictStrAny) -> None:
        dirname = self.pathlib.dirname(filepath)
        if not self.fs_client.isdir(dirname):
            return
        self.fs_client.write_text(filepath, json.dumps(data), "utf-8")

    def _to_path_safe_string(self, s: str) -> str:
        """for base64 strings"""
        return base64.b64decode(s).hex() if s else None

    def _list_dlt_table_files(self, table_name: str) -> Iterator[Tuple[str, List[str]]]:
        dirname = self.get_table_dir(table_name)
        if not self.fs_client.exists(self.pathlib.join(dirname, INIT_FILE_NAME)):
            raise DestinationUndefinedEntity({"dir": dirname})
        for filepath in self.list_table_files(table_name):
            filename = os.path.splitext(os.path.basename(filepath))[0]
            fileparts = filename.split(FILENAME_SEPARATOR)
            if len(fileparts) != 3:
                continue
            yield filepath, fileparts

    def _store_load(self, load_id: str) -> None:
        # write entry to load "table"
        # TODO: this is also duplicate across all destinations. DRY this.
        load_data = {
            "load_id": load_id,
            "schema_name": self.schema.name,
            "status": 0,
            "inserted_at": pendulum.now().isoformat(),
            "schema_version_hash": self.schema.version_hash,
        }
        filepath = self.pathlib.join(
            self.dataset_path,
            self.schema.loads_table_name,
            f"{self.schema.name}{FILENAME_SEPARATOR}{load_id}.jsonl",
        )
        self._write_to_json_file(filepath, load_data)

    def complete_load(self, load_id: str) -> None:
        # store current state
        self._store_current_state(load_id)
        self._store_load(load_id)

    #
    # state read/write
    #

    def _get_state_file_name(self, pipeline_name: str, version_hash: str, load_id: str) -> str:
        """gets full path for schema file for a given hash"""
        return self.pathlib.join(  # type: ignore[no-any-return]
            self.get_table_dir(self.schema.state_table_name),
            f"{pipeline_name}{FILENAME_SEPARATOR}{load_id}{FILENAME_SEPARATOR}{self._to_path_safe_string(version_hash)}.jsonl",
        )

    def _store_current_state(self, load_id: str) -> None:
        # don't save the state this way when used as staging
        if self.config.as_staging:
            return
        # get state doc from current pipeline
        from dlt.pipeline.current import load_package

        pipeline_state_doc = load_package()["state"].get("pipeline_state")

        if not pipeline_state_doc:
            return

        # get paths
        pipeline_name = pipeline_state_doc["pipeline_name"]
        hash_path = self._get_state_file_name(
            pipeline_name, self.schema.stored_version_hash, load_id
        )

        # write
        self._write_to_json_file(hash_path, cast(DictStrAny, pipeline_state_doc))

    def get_stored_state(self, pipeline_name: str) -> Optional[StateInfo]:
        # search newest state
        selected_path = None
        newest_load_id = "0"
        for filepath, fileparts in self._list_dlt_table_files(self.schema.state_table_name):
            if fileparts[0] == pipeline_name and fileparts[1] > newest_load_id:
                newest_load_id = fileparts[1]
                selected_path = filepath

        # Load compressed state from destination
        if selected_path:
            state_json = json.loads(self.fs_client.read_text(selected_path))
            state_json.pop("version_hash")
            return StateInfo(**state_json)

        return None

    #
    # Schema read/write
    #

    def _get_schema_file_name(self, version_hash: str, load_id: str) -> str:
        """gets full path for schema file for a given hash"""

        return self.pathlib.join(  # type: ignore[no-any-return]
            self.get_table_dir(self.schema.version_table_name),
            f"{self.schema.name}{FILENAME_SEPARATOR}{load_id}{FILENAME_SEPARATOR}{self._to_path_safe_string(version_hash)}.jsonl",
        )

    def _get_stored_schema_by_hash_or_newest(
        self, version_hash: str = None
    ) -> Optional[StorageSchemaInfo]:
        """Get the schema by supplied hash, falls back to getting the newest version matching the existing schema name"""
        version_hash = self._to_path_safe_string(version_hash)
        # find newest schema for pipeline or by version hash
        selected_path = None
        newest_load_id = "0"
        for filepath, fileparts in self._list_dlt_table_files(self.schema.version_table_name):
            if (
                not version_hash
                and fileparts[0] == self.schema.name
                and fileparts[1] > newest_load_id
            ):
                newest_load_id = fileparts[1]
                selected_path = filepath
            elif fileparts[2] == version_hash:
                selected_path = filepath
                break

        if selected_path:
            return StorageSchemaInfo(**json.loads(self.fs_client.read_text(selected_path)))

        return None

    def _store_current_schema(self) -> None:
        # check if schema with hash exists
        current_hash = self.schema.stored_version_hash
        if self._get_stored_schema_by_hash_or_newest(current_hash):
            return

        # get paths
        schema_id = str(time.precise_time())
        filepath = self._get_schema_file_name(self.schema.stored_version_hash, schema_id)

        # TODO: duplicate of weaviate implementation, should be abstracted out
        version_info = {
            "version_hash": self.schema.stored_version_hash,
            "schema_name": self.schema.name,
            "version": self.schema.version,
            "engine_version": self.schema.ENGINE_VERSION,
            "inserted_at": pendulum.now(),
            "schema": json.dumps(self.schema.to_dict()),
        }

        # we always keep tabs on what the current schema is
        self._write_to_json_file(filepath, version_info)

    def get_stored_schema(self) -> Optional[StorageSchemaInfo]:
        """Retrieves newest schema from destination storage"""
        return self._get_stored_schema_by_hash_or_newest()

    def get_stored_schema_by_hash(self, version_hash: str) -> Optional[StorageSchemaInfo]:
        return self._get_stored_schema_by_hash_or_newest(version_hash)

    def _write_delta_table(
        self, path: str, table: pa.Table, write_disposition: TWriteDisposition
    ) -> None:
        """Writes in-memory Arrow table to on-disk Delta table."""

        def adjust_arrow_schema(
            schema: pa.Schema,
            type_map: Dict[Callable[[pa.DataType], bool], Callable[..., pa.DataType]],
        ) -> pa.Schema:
            """Returns adjusted Arrow schema.

            Replaces data types for fields matching a type check in `type_map`.
            Type check functions in `type_map` are assumed to be mutually exclusive, i.e.
            a data type does not match more than one type check function.
            """
            for i, e in enumerate(schema.types):
                for type_check, cast_type in type_map.items():
                    if type_check(e):
                        adjusted_field = schema.field(i).with_type(cast_type)
                        schema = schema.set(i, adjusted_field)
                        break  # if type matches type check, do not do other type checks
            return schema

        def ensure_delta_compatible_arrow_table(table: pa.table) -> pa.Table:
            """Returns Arrow table compatible with Delta table format.

            Casts table schema to replace data types not supported by Delta.
            """
            ARROW_TO_DELTA_COMPATIBLE_ARROW_TYPE_MAP = {
                # maps type check function to type factory function
                pa.types.is_null: pa.string(),
                pa.types.is_time: pa.string(),
                pa.types.is_decimal256: (
                    pa.string()
                ),  # pyarrow does not allow downcasting to decimal128
            }
            adjusted_schema = adjust_arrow_schema(
                table.schema, ARROW_TO_DELTA_COMPATIBLE_ARROW_TYPE_MAP
            )
            return table.cast(adjusted_schema)

        def get_delta_write_mode(write_disposition: TWriteDisposition) -> str:
            """Translates dlt write disposition to Delta write mode."""
            if write_disposition in ("append", "merge"):  # `merge` disposition resolves to `append`
                return "append"
            elif write_disposition == "replace":
                return "overwrite"
            else:
                raise ValueError(
                    "`write_disposition` must be `append`, `replace`, or `merge`,"
                    f" but `{write_disposition}` was provided."
                )

        # throws warning for `s3` protocol: https://github.com/delta-io/delta-rs/issues/2460
        # TODO: upgrade `deltalake` lib after https://github.com/delta-io/delta-rs/pull/2500
        # is released
        write_deltalake(  # type: ignore[call-overload]
            table_or_uri=path,
            data=ensure_delta_compatible_arrow_table(table),
            mode=get_delta_write_mode(write_disposition),
            schema_mode="merge",  # enable schema evolution (adding new columns)
            storage_options=self._deltalake_storage_options,
            engine="rust",  # `merge` schema mode requires `rust` engine
        )

    @property
    def _deltalake_storage_options(self) -> Dict[str, str]:
        """Returns dict that can be passed as `storage_options` in `deltalake` library."""
        # TODO: implement method properly and remove hard-coded values.
        # `delta-rs` does not currently (version 0.17.4) support `gdrive` protocol
        storage_options = None
        if self.config.protocol == "s3":
            from dlt.common.configuration.specs import AwsCredentials

            assert isinstance(self.config.credentials, AwsCredentials)
            storage_options = self.config.credentials.to_session_credentials()
            storage_options["AWS_REGION"] = self.config.credentials.region_name
            # https://delta-io.github.io/delta-rs/usage/writing/writing-to-s3-with-locking-provider/#enable-unsafe-writes-in-s3-opt-in
            storage_options["AWS_S3_ALLOW_UNSAFE_RENAME"] = "true"
        elif self.config.protocol == "gs":
            from dlt.common.configuration.specs import GcpServiceAccountCredentials

            assert isinstance(self.config.credentials, GcpServiceAccountCredentials)
            gcs_creds = self.config.credentials.to_gcs_credentials()
            gcs_creds["token"]["private_key_id"] = "921837921798379812"
            storage_options = {"GOOGLE_SERVICE_ACCOUNT_KEY": json.dumps(gcs_creds["token"])}
        else:
            storage_options = {  # `delta-rs` requires string values
                k: str(v) for k, v in self.fs_client.storage_options.items()
            }
        return storage_options

---
title: Snowflake
description: Snowflake `dlt` destination
keywords: [Snowflake, destination, data warehouse]
---

# Snowflake

## Install `dlt` with Snowflake
**To install the `dlt` library with Snowflake dependencies, run:**
```sh
pip install "dlt[snowflake]"
```

## Setup Guide

**1. Initialize a project with a pipeline that loads to Snowflake by running:**
```sh
dlt init chess snowflake
```

**2. Install the necessary dependencies for Snowflake by running:**
```sh
pip install -r requirements.txt
```
This will install `dlt` with the `snowflake` extra, which contains the Snowflake Python dbapi client.

**3. Create a new database, user, and give `dlt` access.**

Read the next chapter below.

**4. Enter your credentials into `.dlt/secrets.toml`.**
It should now look like this:
```toml
[destination.snowflake.credentials]
database = "dlt_data"
password = "<password>"
username = "loader"
host = "kgiotue-wn98412"
warehouse = "COMPUTE_WH"
role = "DLT_LOADER_ROLE"
```
In the case of Snowflake, the **host** is your [Account Identifier](https://docs.snowflake.com/en/user-guide/admin-account-identifier). You can get it in **Admin**/**Accounts** by copying the account URL: https://kgiotue-wn98412.snowflakecomputing.com and extracting the host name (**kgiotue-wn98412**).

The **warehouse** and **role** are optional if you assign defaults to your user. In the example below, we do not do that, so we set them explicitly.

### Setup the database user and permissions
The instructions below assume that you use the default account setup that you get after creating a Snowflake account. You should have a default warehouse named **COMPUTE_WH** and a Snowflake account. Below, we create a new database, user, and assign permissions. The permissions are very generous. A more experienced user can easily reduce `dlt` permissions to just one schema in the database.
```sql
--create database with standard settings
CREATE DATABASE dlt_data;
-- create new user - set your password here
CREATE USER loader WITH PASSWORD='<password>'
-- we assign all permission to a role
CREATE ROLE DLT_LOADER_ROLE;
GRANT ROLE DLT_LOADER_ROLE TO USER loader;
-- give database access to new role
GRANT USAGE ON DATABASE dlt_data TO DLT_LOADER_ROLE;
-- allow `dlt` to create new schemas
GRANT CREATE SCHEMA ON DATABASE dlt_data TO ROLE DLT_LOADER_ROLE
-- allow access to a warehouse named COMPUTE_WH
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO DLT_LOADER_ROLE;
-- grant access to all future schemas and tables in the database
GRANT ALL PRIVILEGES ON FUTURE SCHEMAS IN DATABASE dlt_data TO DLT_LOADER_ROLE;
GRANT ALL PRIVILEGES ON FUTURE TABLES IN DATABASE dlt_data TO DLT_LOADER_ROLE;
```

Now you can use the user named `LOADER` to access the database `DLT_DATA` and log in with the specified password.

You can also decrease the suspend time for your warehouse to 1 minute (**Admin**/**Warehouses** in Snowflake UI)

### Authentication types
Snowflake destination accepts three authentication types:
- password authentication
- [key pair authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth)
- external authentication

The **password authentication** is not any different from other databases like Postgres or Redshift. `dlt` follows the same syntax as the [SQLAlchemy dialect](https://docs.snowflake.com/en/developer-guide/python-connector/sqlalchemy#required-parameters).

You can also pass credentials as a database connection string. For example:
```toml
# keep it at the top of your toml file! before any section starts
destination.snowflake.credentials="snowflake://loader:<password>@kgiotue-wn98412/dlt_data?warehouse=COMPUTE_WH&role=DLT_LOADER_ROLE"
```

In **key pair authentication**, you replace the password with a private key string that should be in Base64-encoded DER format ([DBT also recommends](https://docs.getdbt.com/docs/core/connect-data-platform/snowflake-setup#key-pair-authentication) base64-encoded private keys for Snowflake connections). The private key may also be encrypted. In that case, you must provide a passphrase alongside the private key.
```toml
[destination.snowflake.credentials]
database = "dlt_data"
username = "loader"
host = "kgiotue-wn98412"
private_key = "LS0tLS1CRUdJTiBFTkNSWVBURUQgUFJJ....Qo="
private_key_passphrase="passphrase"
```
> You can easily get the base64-encoded value of your private key by running `base64 -i <path-to-private-key-file>.pem` in your terminal

If you pass a passphrase in the connection string, please URL encode it.
```toml
# keep it at the top of your toml file! before any section starts
destination.snowflake.credentials="snowflake://loader:<password>@kgiotue-wn98412/dlt_data?private_key=<base64 encoded pem>&private_key_passphrase=<url encoded passphrase>"
```

In **external authentication**, you can use an OAuth provider like Okta or an external browser to authenticate. You pass your authenticator and refresh token as below:
```toml
[destination.snowflake.credentials]
database = "dlt_data"
username = "loader"
authenticator="..."
token="..."
```
or in the connection string as query parameters.
Refer to Snowflake [OAuth](https://docs.snowflake.com/en/user-guide/oauth-intro) for more details.

## Write disposition
All write dispositions are supported.

If you set the [`replace` strategy](../../general-usage/full-loading.md) to `staging-optimized`, the destination tables will be dropped and
recreated with a [clone command](https://docs.snowflake.com/en/sql-reference/sql/create-clone) from the staging tables.

## Data loading
The data is loaded using an internal Snowflake stage. We use the `PUT` command and per-table built-in stages by default. Stage files are immediately removed (if not specified otherwise).

## Supported file formats
* [insert-values](../file-formats/insert-format.md) is used by default
* [parquet](../file-formats/parquet.md) is supported
* [jsonl](../file-formats/jsonl.md) is supported

When staging is enabled:
* [jsonl](../file-formats/jsonl.md) is used by default
* [parquet](../file-formats/parquet.md) is supported

> ❗ When loading from `parquet`, Snowflake will store `complex` types (JSON) in `VARIANT` as a string. Use the `jsonl` format instead or use `PARSE_JSON` to update the `VARIANT` field after loading.

## Supported column hints
Snowflake supports the following [column hints](https://dlthub.com/docs/general-usage/schema#tables-and-columns):
* `cluster` - creates a cluster column(s). Many columns per table are supported and only when a new table is created.

### Table and column identifiers
Snowflake makes all unquoted identifiers uppercase and then resolves them case-insensitively in SQL statements. `dlt` (effectively) does not quote identifiers in DDL, preserving default behavior.

Names of tables and columns in [schemas](../../general-usage/schema.md) are kept in lower case like for all other destinations. This is the pattern we observed in other tools, i.e., `dbt`. In the case of `dlt`, it is, however, trivial to define your own uppercase [naming convention](../../general-usage/schema.md#naming-convention)

## Staging support

Snowflake supports S3 and GCS as file staging destinations. `dlt` will upload files in the parquet format to the bucket provider and will ask Snowflake to copy their data directly into the db.

Alternatively to parquet files, you can also specify jsonl as the staging file format. For this, set the `loader_file_format` argument of the `run` command of the pipeline to `jsonl`.

### Snowflake and Amazon S3

Please refer to the [S3 documentation](./filesystem.md#aws-s3) to learn how to set up your bucket with the bucket_url and credentials. For S3, the `dlt` Redshift loader will use the AWS credentials provided for S3 to access the S3 bucket if not specified otherwise (see config options below). Alternatively, you can create a stage for your S3 Bucket by following the instructions provided in the [Snowflake S3 documentation](https://docs.snowflake.com/en/user-guide/data-load-s3-config-storage-integration).
The basic steps are as follows:

* Create a storage integration linked to GCS and the right bucket
* Grant access to this storage integration to the Snowflake role you are using to load the data into Snowflake.
* Create a stage from this storage integration in the PUBLIC namespace, or the namespace of the schema of your data.
* Also grant access to this stage for the role you are using to load data into Snowflake.
* Provide the name of your stage (including the namespace) to `dlt` like so:

To prevent `dlt` from forwarding the S3 bucket credentials on every command, and set your S3 stage, change these settings:

```toml
[destination]
stage_name="PUBLIC.my_s3_stage"
```

To run Snowflake with S3 as the staging destination:

```py
# Create a `dlt` pipeline that will load
# chess player data to the Snowflake destination
# via staging on S3
pipeline = dlt.pipeline(
    pipeline_name='chess_pipeline',
    destination='snowflake',
    staging='filesystem', # add this to activate the staging location
    dataset_name='player_data'
)
```

### Snowflake and Google Cloud Storage

Please refer to the [Google Storage filesystem documentation](./filesystem.md#google-storage) to learn how to set up your bucket with the bucket_url and credentials. For GCS, you can define a stage in Snowflake and provide the stage identifier in the configuration (see config options below.) Please consult the Snowflake Documentation on [how to create a stage for your GCS Bucket](https://docs.snowflake.com/en/user-guide/data-load-gcs-config). The basic steps are as follows:

* Create a storage integration linked to GCS and the right bucket
* Grant access to this storage integration to the Snowflake role you are using to load the data into Snowflake.
* Create a stage from this storage integration in the PUBLIC namespace, or the namespace of the schema of your data.
* Also grant access to this stage for the role you are using to load data into Snowflake.
* Provide the name of your stage (including the namespace) to `dlt` like so:

```toml
[destination]
stage_name="PUBLIC.my_gcs_stage"
```

To run Snowflake with GCS as the staging destination:

```py
# Create a `dlt` pipeline that will load
# chess player data to the Snowflake destination
# via staging on GCS
pipeline = dlt.pipeline(
    pipeline_name='chess_pipeline',
    destination='snowflake',
    staging='filesystem', # add this to activate the staging location
    dataset_name='player_data'
)
```

### Snowflake and Azure Blob Storage

Please refer to the [Azure Blob Storage filesystem documentation](./filesystem.md#azure-blob-storage) to learn how to set up your bucket with the bucket_url and credentials. For Azure, the Snowflake loader will use
the filesystem credentials for your Azure Blob Storage container if not specified otherwise (see config options below). Alternatively, you can define an external stage in Snowflake and provide the stage identifier.
Please consult the Snowflake Documentation on [how to create a stage for your Azure Blob Storage Container](https://docs.snowflake.com/en/user-guide/data-load-azure). The basic steps are as follows:

* Create a storage integration linked to Azure Blob Storage and the right container
* Grant access to this storage integration to the Snowflake role you are using to load the data into Snowflake.
* Create a stage from this storage integration in the PUBLIC namespace, or the namespace of the schema of your data.
* Also grant access to this stage for the role you are using to load data into Snowflake.
* Provide the name of your stage (including the namespace) to `dlt` like so:

```toml
[destination]
stage_name="PUBLIC.my_azure_stage"
```

To run Snowflake with Azure as the staging destination:

```py
# Create a `dlt` pipeline that will load
# chess player data to the Snowflake destination
# via staging on Azure
pipeline = dlt.pipeline(
    pipeline_name='chess_pipeline',
    destination='snowflake',
    staging='filesystem', # add this to activate the staging location
    dataset_name='player_data'
)
```

## Additional destination options
You can define your own stage to PUT files and disable the removal of the staged files after loading.
```toml
[destination.snowflake]
# Use an existing named stage instead of the default. Default uses the implicit table stage per table
stage_name="DLT_STAGE"
# Whether to keep or delete the staged files after COPY INTO succeeds
keep_staged_files=true
```

### dbt support
This destination [integrates with dbt](../transformations/dbt/dbt.md) via [dbt-snowflake](https://github.com/dbt-labs/dbt-snowflake). Both password and key pair authentication are supported and shared with dbt runners.

### Syncing of `dlt` state
This destination fully supports [dlt state sync](../../general-usage/state#syncing-state-with-destination)

### Snowflake connection identifier
We enable Snowflake to identify that the connection is created by `dlt`. Snowflake will use this identifier to better understand the usage patterns
associated with `dlt` integration. The connection identifier is `dltHub_dlt`.

<!--@@@DLT_TUBA snowflake-->


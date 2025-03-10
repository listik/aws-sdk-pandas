from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import boto3
import pyarrow as pa
import pyodbc
import pytest

import awswrangler as wr
import awswrangler.pandas as pd
from awswrangler import _databases as _db_utils

from .._utils import ensure_data_types, get_df, pandas_equals

logging.getLogger("awswrangler").setLevel(logging.DEBUG)

pytestmark = pytest.mark.distributed


@pytest.fixture(scope="module", autouse=True)
def create_sql_server_database(databases_parameters: dict[str, Any]) -> None:
    attrs = _db_utils.get_connection_attributes(connection="aws-sdk-pandas-sqlserver")
    connection_str = (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={attrs.host},{attrs.port};"
        f"UID={attrs.user};"
        f"PWD={attrs.password}"
    )

    database_name = databases_parameters["sqlserver"]["database"]
    sql_create_db = (
        f"IF NOT EXISTS(SELECT * FROM sys.databases WHERE name = '{database_name}') "
        "BEGIN "
        f"CREATE DATABASE {database_name} "
        "END"
    )

    with pyodbc.connect(connection_str, autocommit=True) as con:
        with con.cursor() as cursor:
            cursor.execute(sql_create_db)
            con.commit()


@pytest.fixture(scope="function")
def sqlserver_con():
    con = wr.sqlserver.connect("aws-sdk-pandas-sqlserver")
    yield con
    con.close()


def test_connection():
    wr.sqlserver.connect("aws-sdk-pandas-sqlserver", timeout=10).close()


def test_read_sql_query_simple(databases_parameters, sqlserver_con):
    df = wr.sqlserver.read_sql_query("SELECT 1", con=sqlserver_con)
    assert df.shape == (1, 1)


def test_to_sql_simple(sqlserver_table, sqlserver_con):
    df = pd.DataFrame({"c0": [1, 2, 3], "c1": ["foo", "boo", "bar"]})
    wr.sqlserver.to_sql(df, sqlserver_con, sqlserver_table, "dbo", "overwrite", True)


def test_sql_types(sqlserver_table, sqlserver_con):
    table = sqlserver_table
    df = get_df()
    df.drop(["binary"], axis=1, inplace=True)
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        table=table,
        schema="dbo",
        mode="overwrite",
        index=True,
        dtype={"iint32": "INTEGER"},
    )
    df = wr.sqlserver.read_sql_query(f"SELECT * FROM dbo.{table}", sqlserver_con)
    ensure_data_types(df, has_list=False)
    dfs = wr.sqlserver.read_sql_query(
        sql=f"SELECT * FROM dbo.{table}",
        con=sqlserver_con,
        chunksize=1,
        dtype={
            "iint8": pa.int8(),
            "iint16": pa.int16(),
            "iint32": pa.int32(),
            "iint64": pa.int64(),
            "float": pa.float32(),
            "ddouble": pa.float64(),
            "decimal": pa.decimal128(3, 2),
            "string_object": pa.string(),
            "string": pa.string(),
            "date": pa.date32(),
            "timestamp": pa.timestamp(unit="ns"),
            "binary": pa.binary(),
            "category": pa.float64(),
        },
    )
    for df in dfs:
        ensure_data_types(df, has_list=False)


def test_to_sql_cast(sqlserver_table, sqlserver_con):
    table = sqlserver_table
    df = pd.DataFrame(
        {
            "col": [
                "".join([str(i)[-1] for i in range(1_024)]),
                "".join([str(i)[-1] for i in range(1_024)]),
                "".join([str(i)[-1] for i in range(1_024)]),
            ]
        },
        dtype="string",
    )
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        table=table,
        schema="dbo",
        mode="overwrite",
        index=False,
        dtype={"col": "VARCHAR(1024)"},
    )
    df2 = wr.sqlserver.read_sql_query(sql=f"SELECT * FROM dbo.{table}", con=sqlserver_con)
    assert df.equals(df2)


def test_to_sql_fast_executemany(sqlserver_table, sqlserver_con):
    df = pd.DataFrame({"c0": [1, 2, 3]}, dtype="Int64")
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        table=sqlserver_table,
        schema="dbo",
        mode="overwrite",
        fast_executemany=True,
    )
    df2 = wr.sqlserver.read_sql_table(
        table=sqlserver_table,
        con=sqlserver_con,
        schema="dbo",
    )
    assert df.equals(df2)


def test_null(sqlserver_table, sqlserver_con):
    table = sqlserver_table
    df = pd.DataFrame({"id": [1, 2, 3], "nothing": [None, None, None]})
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        table=table,
        schema="dbo",
        mode="overwrite",
        index=False,
        dtype={"nothing": "INTEGER"},
    )
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        table=table,
        schema="dbo",
        mode="append",
        index=False,
    )
    df2 = wr.sqlserver.read_sql_table(table=table, schema="dbo", con=sqlserver_con)
    df["id"] = df["id"].astype("Int64")
    assert pandas_equals(pd.concat(objs=[df, df], ignore_index=True), df2)


def test_decimal_cast(sqlserver_table, sqlserver_con):
    table = sqlserver_table
    df = pd.DataFrame(
        {
            "col0": [Decimal((0, (1, 9, 9), -2)), None, Decimal((0, (1, 9, 0), -2))],
            "col1": [Decimal((0, (1, 9, 9), -2)), None, Decimal((0, (1, 9, 0), -2))],
            "col2": [Decimal((0, (1, 9, 9), -2)), None, Decimal((0, (1, 9, 0), -2))],
        }
    )
    wr.sqlserver.to_sql(df, sqlserver_con, table, "dbo")
    df2 = wr.sqlserver.read_sql_table(
        schema="dbo", table=table, con=sqlserver_con, dtype={"col0": "float32", "col1": "float64", "col2": "Int64"}
    )
    assert df2.dtypes.to_list() == ["float32", "float64", "Int64"]
    assert 3.88 <= df2.col0.sum() <= 3.89
    assert 3.88 <= df2.col1.sum() <= 3.89
    assert df2.col2.sum() == 2


def test_read_retry(sqlserver_con):
    try:
        wr.sqlserver.read_sql_query("ERROR", sqlserver_con)
    except:  # noqa
        pass
    df = wr.sqlserver.read_sql_query("SELECT 1", sqlserver_con)
    assert df.shape == (1, 1)


def test_table_name(sqlserver_con):
    df = pd.DataFrame({"col0": [1]})
    wr.sqlserver.to_sql(df, sqlserver_con, "Test Name", "dbo", mode="overwrite")
    df = wr.sqlserver.read_sql_table(schema="dbo", con=sqlserver_con, table="Test Name")
    assert df.shape == (1, 1)
    with sqlserver_con.cursor() as cursor:
        cursor.execute('DROP TABLE "Test Name"')
    sqlserver_con.commit()


@pytest.mark.parametrize("dbname", [None, "test"])
def test_connect_secret_manager(dbname):
    try:
        con = wr.sqlserver.connect(secret_id="aws-sdk-pandas/sqlserver", dbname=dbname)
        df = wr.sqlserver.read_sql_query("SELECT 1", con=con)
        assert df.shape == (1, 1)
    except boto3.client("secretsmanager").exceptions.ResourceNotFoundException:
        pass  # Workaround for secretmanager inconsistance


def test_insert_with_column_names(sqlserver_table, sqlserver_con):
    create_table_sql = (
        f"CREATE TABLE dbo.{sqlserver_table} (c0 varchar(100) NULL,c1 INT DEFAULT 42 NULL,c2 INT NOT NULL);"
    )
    with sqlserver_con.cursor() as cursor:
        cursor.execute(create_table_sql)
        sqlserver_con.commit()

    df = pd.DataFrame({"c0": ["foo", "bar"], "c2": [1, 2]})

    with pytest.raises(pyodbc.Error):
        wr.sqlserver.to_sql(
            df=df, con=sqlserver_con, schema="dbo", table=sqlserver_table, mode="append", use_column_names=False
        )

    wr.sqlserver.to_sql(
        df=df, con=sqlserver_con, schema="dbo", table=sqlserver_table, mode="append", use_column_names=True
    )

    df2 = wr.sqlserver.read_sql_table(con=sqlserver_con, schema="dbo", table=sqlserver_table)

    df["c1"] = 42
    df["c0"] = df["c0"].astype("string")
    df["c1"] = df["c1"].astype("Int64")
    df["c2"] = df["c2"].astype("Int64")
    df = df.reindex(sorted(df.columns), axis=1)
    assert df.equals(df2)


@pytest.mark.parametrize("chunksize", [1, 10, 500])
def test_dfs_are_equal_for_different_chunksizes(sqlserver_table, sqlserver_con, chunksize):
    df = pd.DataFrame({"c0": [i for i in range(64)], "c1": ["foo" for _ in range(64)]})
    wr.sqlserver.to_sql(df=df, con=sqlserver_con, schema="dbo", table=sqlserver_table, chunksize=chunksize)

    df2 = wr.sqlserver.read_sql_table(con=sqlserver_con, schema="dbo", table=sqlserver_table)

    df["c0"] = df["c0"].astype("Int64")
    df["c1"] = df["c1"].astype("string")

    assert df.equals(df2)


def test_upsert(sqlserver_table, sqlserver_con):
    df = pd.DataFrame({"c0": ["foo", "bar"], "c2": [1, 2]})

    with pytest.raises(wr.exceptions.InvalidArgumentValue):
        wr.sqlserver.to_sql(
            df=df,
            con=sqlserver_con,
            schema="dbo",
            table=sqlserver_table,
            mode="upsert",
            upsert_conflict_columns=None,
            use_column_names=True,
        )

    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        schema="dbo",
        table=sqlserver_table,
        mode="upsert",
        upsert_conflict_columns=["c0"],
    )
    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        schema="dbo",
        table=sqlserver_table,
        mode="upsert",
        upsert_conflict_columns=["c0"],
    )
    df2 = wr.sqlserver.read_sql_table(con=sqlserver_con, schema="dbo", table=sqlserver_table)
    assert bool(len(df2) == 2)

    wr.sqlserver.to_sql(
        df=df,
        con=sqlserver_con,
        schema="dbo",
        table=sqlserver_table,
        mode="upsert",
        upsert_conflict_columns=["c0"],
    )
    df3 = pd.DataFrame({"c0": ["baz", "bar"], "c2": [3, 2]})
    wr.sqlserver.to_sql(
        df=df3,
        con=sqlserver_con,
        schema="dbo",
        table=sqlserver_table,
        mode="upsert",
        upsert_conflict_columns=["c0"],
        use_column_names=True,
    )
    df4 = wr.sqlserver.read_sql_table(con=sqlserver_con, schema="dbo", table=sqlserver_table)
    assert bool(len(df4) == 3)

    df5 = pd.DataFrame({"c0": ["foo", "bar"], "c2": [4, 5]})
    wr.sqlserver.to_sql(
        df=df5,
        con=sqlserver_con,
        schema="dbo",
        table=sqlserver_table,
        mode="upsert",
        upsert_conflict_columns=["c0"],
        use_column_names=True,
    )

    df6 = wr.sqlserver.read_sql_table(con=sqlserver_con, schema="dbo", table=sqlserver_table)
    assert bool(len(df6) == 3)
    assert bool(len(df6.loc[(df6["c0"] == "foo") & (df6["c2"] == 4)]) == 1)
    assert bool(len(df6.loc[(df6["c0"] == "bar") & (df6["c2"] == 5)]) == 1)

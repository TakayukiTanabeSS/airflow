# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.providers.amazon.aws.hooks.redshift_data import RedshiftDataHook
from airflow.providers.amazon.aws.hooks.redshift_sql import RedshiftSQLHook
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.amazon.aws.utils.redshift import build_credentials_block

if TYPE_CHECKING:
    from airflow.utils.context import Context

AVAILABLE_METHODS = ["APPEND", "REPLACE", "UPSERT"]


class S3ToRedshiftOperator(BaseOperator):
    """
    Executes an COPY command to load files from s3 to Redshift.

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:S3ToRedshiftOperator`

    :param table: reference to a specific table in redshift database
    :param s3_bucket: reference to a specific S3 bucket
    :param s3_key: key prefix that selects single or multiple objects from S3
    :param schema: reference to a specific schema in redshift database.
        Do not provide when copying into a temporary table
    :param redshift_conn_id: reference to a specific redshift database OR a redshift data-api connection
    :param aws_conn_id: reference to a specific S3 connection
        If the AWS connection contains 'aws_iam_role' in ``extras``
        the operator will use AWS STS credentials with a token
        https://docs.aws.amazon.com/redshift/latest/dg/copy-parameters-authorization.html#copy-credentials
    :param verify: Whether to verify SSL certificates for S3 connection.
        By default, SSL certificates are verified.
        You can provide the following values:

        - ``False``: do not validate SSL certificates. SSL will still be used
                 (unless use_ssl is False), but SSL certificates will not be
                 verified.
        - ``path/to/cert/bundle.pem``: A filename of the CA cert bundle to uses.
                 You can specify this argument if you want to use a different
                 CA cert bundle than the one used by botocore.
    :param column_list: list of column names to load source data fields into specific target columns
        https://docs.aws.amazon.com/redshift/latest/dg/copy-parameters-column-mapping.html#copy-column-list
    :param copy_options: reference to a list of COPY options
    :param method: Action to be performed on execution. Available ``APPEND``, ``UPSERT`` and ``REPLACE``.
    :param upsert_keys: List of fields to use as key on upsert action
    :param redshift_data_api_kwargs: If using the Redshift Data API instead of the SQL-based connection,
        dict of arguments for the hook's ``execute_query`` method.
        Cannot include any of these kwargs: ``{'sql', 'parameters'}``
    """

    template_fields: Sequence[str] = (
        "s3_bucket",
        "s3_key",
        "schema",
        "table",
        "column_list",
        "copy_options",
        "redshift_conn_id",
        "method",
        "redshift_data_api_kwargs",
        "aws_conn_id",
    )
    template_ext: Sequence[str] = ()
    ui_color = "#99e699"

    def __init__(
        self,
        *,
        table: str,
        s3_bucket: str,
        s3_key: str,
        schema: str | None = None,
        redshift_conn_id: str = "redshift_default",
        aws_conn_id: str | None = "aws_default",
        verify: bool | str | None = None,
        column_list: list[str] | None = None,
        copy_options: list | None = None,
        autocommit: bool = False,
        method: str = "APPEND",
        upsert_keys: list[str] | None = None,
        redshift_data_api_kwargs: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.schema = schema
        self.table = table
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.redshift_conn_id = redshift_conn_id
        self.aws_conn_id = aws_conn_id
        self.verify = verify
        self.column_list = column_list
        self.copy_options = copy_options or []
        self.autocommit = autocommit
        self.method = method
        self.upsert_keys = upsert_keys
        self.redshift_data_api_kwargs = redshift_data_api_kwargs or {}

        if self.redshift_data_api_kwargs:
            for arg in ["sql", "parameters"]:
                if arg in self.redshift_data_api_kwargs:
                    raise AirflowException(f"Cannot include param '{arg}' in Redshift Data API kwargs")

    @property
    def use_redshift_data(self):
        return bool(self.redshift_data_api_kwargs)

    def _build_copy_query(
        self, copy_destination: str, credentials_block: str, region_info: str, copy_options: str
    ) -> str:
        column_names = "(" + ", ".join(self.column_list) + ")" if self.column_list else ""
        return f"""
                    COPY {copy_destination} {column_names}
                    FROM 's3://{self.s3_bucket}/{self.s3_key}'
                    credentials
                    '{credentials_block}'
                    {region_info}
                    {copy_options};
        """

    def execute(self, context: Context) -> None:
        if self.method not in AVAILABLE_METHODS:
            raise AirflowException(f"Method not found! Available methods: {AVAILABLE_METHODS}")

        if self.use_redshift_data:
            redshift_data_hook = RedshiftDataHook(aws_conn_id=self.redshift_conn_id)
        else:
            redshift_sql_hook = RedshiftSQLHook(redshift_conn_id=self.redshift_conn_id)

        conn = S3Hook.get_connection(conn_id=self.aws_conn_id) if self.aws_conn_id else None
        region_info = ""
        if conn and conn.extra_dejson.get("region", False):
            region_info = f"region '{conn.extra_dejson['region']}'"
        if conn and conn.extra_dejson.get("role_arn", False):
            credentials_block = f"aws_iam_role={conn.extra_dejson['role_arn']}"
        else:
            s3_hook = S3Hook(aws_conn_id=self.aws_conn_id, verify=self.verify)
            credentials = s3_hook.get_credentials()
            credentials_block = build_credentials_block(credentials)

        copy_options = "\n\t\t\t".join(self.copy_options)
        destination = f"{self.schema}.{self.table}" if self.schema else self.table
        copy_destination = f"#{self.table}" if self.method == "UPSERT" else destination

        copy_statement = self._build_copy_query(
            copy_destination, credentials_block, region_info, copy_options
        )

        sql: str | Iterable[str]

        if self.method == "REPLACE":
            sql = ["BEGIN;", f"DELETE FROM {destination};", copy_statement, "COMMIT"]
        elif self.method == "UPSERT":
            if self.use_redshift_data:
                keys = self.upsert_keys or redshift_data_hook.get_table_primary_key(
                    table=self.table, schema=self.schema, **self.redshift_data_api_kwargs
                )
            else:
                keys = self.upsert_keys or redshift_sql_hook.get_table_primary_key(self.table, self.schema)
            if not keys:
                raise AirflowException(
                    f"No primary key on {self.schema}.{self.table}. Please provide keys on 'upsert_keys'"
                )
            where_statement = " AND ".join([f"{self.table}.{k} = {copy_destination}.{k}" for k in keys])

            sql = [
                f"CREATE TABLE {copy_destination} (LIKE {destination} INCLUDING DEFAULTS);",
                copy_statement,
                "BEGIN;",
                f"DELETE FROM {destination} USING {copy_destination} WHERE {where_statement};",
                f"INSERT INTO {destination} SELECT * FROM {copy_destination};",
                "COMMIT",
            ]

        else:
            sql = copy_statement

        self.log.info("Executing COPY command...")
        if self.use_redshift_data:
            redshift_data_hook.execute_query(sql=sql, **self.redshift_data_api_kwargs)
        else:
            redshift_sql_hook.run(sql, autocommit=self.autocommit)
        self.log.info("COPY command complete...")

    def get_openlineage_facets_on_complete(self, task_instance):
        """Implement on_complete as we will query destination table."""
        from airflow.providers.amazon.aws.utils.openlineage import (
            get_facets_from_redshift_table,
        )
        from airflow.providers.common.compat.openlineage.facet import (
            Dataset,
            LifecycleStateChange,
            LifecycleStateChangeDatasetFacet,
        )
        from airflow.providers.openlineage.extractors import OperatorLineage

        if self.use_redshift_data:
            redshift_data_hook = RedshiftDataHook(aws_conn_id=self.redshift_conn_id)
            database = self.redshift_data_api_kwargs.get("database")
            identifier = self.redshift_data_api_kwargs.get(
                "cluster_identifier", self.redshift_data_api_kwargs.get("workgroup_name")
            )
            port = self.redshift_data_api_kwargs.get("port", "5439")
            authority = f"{identifier}.{redshift_data_hook.region_name}:{port}"
            output_dataset_facets = get_facets_from_redshift_table(
                redshift_data_hook, self.table, self.redshift_data_api_kwargs, self.schema
            )
        else:
            redshift_sql_hook = RedshiftSQLHook(redshift_conn_id=self.redshift_conn_id)
            database = redshift_sql_hook.conn.schema
            authority = redshift_sql_hook.get_openlineage_database_info(redshift_sql_hook.conn).authority
            output_dataset_facets = get_facets_from_redshift_table(
                redshift_sql_hook, self.table, {}, self.schema
            )

        if self.method == "REPLACE":
            output_dataset_facets["lifecycleStateChange"] = LifecycleStateChangeDatasetFacet(
                lifecycleStateChange=LifecycleStateChange.OVERWRITE
            )

        output_dataset = Dataset(
            namespace=f"redshift://{authority}",
            name=f"{database}.{self.schema}.{self.table}" if database else f"{self.schema}.{self.table}",
            facets=output_dataset_facets,
        )

        input_dataset = Dataset(
            namespace=f"s3://{self.s3_bucket}",
            name=self.s3_key,
        )

        return OperatorLineage(inputs=[input_dataset], outputs=[output_dataset])
import importlib
import os
import sys

import boto3
import pytest
from moto import mock_aws

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "infra", "lambda", "src"))

LONG_DESC = (
    "We are hiring a DevOps engineer to own our AWS platform. You will write Terraform modules, run "
    "Kubernetes with Helm, build GitHub Actions CI/CD pipelines, and improve observability with Prometheus "
    "and Grafana. Fully remote across Europe. Linux, Python and Docker experience expected. " * 2
)


@pytest.fixture
def aws():
    with mock_aws():
        os.environ.update(
            AWS_DEFAULT_REGION="eu-west-3", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
            TABLE_NAME="job-radar-jobs", SECRET_PARAM_NAME="/job-radar/ingest-shared-secret",
            INDEX_NAME="pending-index",
        )
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName="job-radar-jobs",
            AttributeDefinitions=[
                {"AttributeName": "job_id", "AttributeType": "S"},
                {"AttributeName": "digest_pk", "AttributeType": "S"},
                {"AttributeName": "score", "AttributeType": "N"},
            ],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "pending-index",
                "KeySchema": [
                    {"AttributeName": "digest_pk", "KeyType": "HASH"},
                    {"AttributeName": "score", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "INCLUDE",
                               "NonKeyAttributes": ["title", "company", "location", "url", "source", "matched", "flags"]},
            }],
            BillingMode="PAY_PER_REQUEST",  # moto only; real table is PROVISIONED (see database.tf)
        )
        ssm = boto3.client("ssm")
        ssm.put_parameter(Name="/job-radar/ingest-shared-secret", Value="s3cret", Type="SecureString")
        topic = boto3.client("sns").create_topic(Name="job-radar")["TopicArn"]
        os.environ["TOPIC_ARN"] = topic
        mods = {}
        for name in ("scoring", "store", "ingest", "fetch", "digest"):
            mod = importlib.import_module(name)
            mods[name] = importlib.reload(mod)
        yield type("Env", (), mods)

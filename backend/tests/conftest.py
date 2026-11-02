import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from clippulse.config import Settings  # noqa: E402
from clippulse.service import ClipService  # noqa: E402
from clippulse.store import DynamoStore, MemoryStore  # noqa: E402
from fakes import FakeClock, FakeTable, FakeTwitch  # noqa: E402

BOARD = "a" * 32


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))


# Set CLIPPULSE_TEST_ENDPOINT (for example http://localhost:8000 with DynamoDB Local running) and
# install boto3 to run every store and service test against a real DynamoDB engine as well.
LOCAL_ENDPOINT = os.environ.get("CLIPPULSE_TEST_ENDPOINT")
STORE_KINDS = ["memory", "dynamo-fake"] + (["dynamodb-local"] if LOCAL_ENDPOINT else [])


def _create_local_table():
    import boto3  # only needed for the opt-in run

    dynamodb = boto3.resource("dynamodb", endpoint_url=LOCAL_ENDPOINT, region_name="us-east-1",
                              aws_access_key_id="test", aws_secret_access_key="test")
    table = dynamodb.create_table(
        TableName=f"clippulse-test-{uuid.uuid4().hex[:10]}", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    table.wait_until_exists()
    return table


@pytest.fixture(params=STORE_KINDS)
def store(request):
    if request.param == "memory":
        yield MemoryStore()
    elif request.param == "dynamo-fake":
        yield DynamoStore(FakeTable(page_size=2))  # tiny pages force the pagination loop to run
    else:
        table = _create_local_table()
        yield DynamoStore(table)
        table.delete()


@pytest.fixture
def twitch():
    return FakeTwitch()


@pytest.fixture
def settings():
    return Settings(max_streamers=3, max_board_items=3)


@pytest.fixture
def service(store, twitch, settings, clock):
    return ClipService(store, lambda: twitch, settings, clock=clock)

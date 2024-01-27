import os
import time

import pytest

# Set before boto3/moto are imported anywhere so nothing can reach a real account.
os.environ.update({
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
})
for name in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_LOGS"):
    os.environ.pop(name, None)


@pytest.fixture
def aws():
    """A fully mocked AWS; yields a boto3 session bound to fake credentials."""
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.Session(region_name="us-east-1")


@pytest.fixture
def put():
    """put(client, group, [(offset_ms_from_now, message), ...]) -> base timestamp in ms."""
    base = int(time.time() * 1000) - 60_000

    def _put(client, group, rows, stream="s1"):
        try:
            client.create_log_group(logGroupName=group)
        except client.exceptions.ResourceAlreadyExistsException:
            pass
        try:
            client.create_log_stream(logGroupName=group, logStreamName=stream)
        except client.exceptions.ResourceAlreadyExistsException:
            pass
        client.put_log_events(logGroupName=group, logStreamName=stream, logEvents=[
            {"timestamp": base + offset, "message": message} for offset, message in rows])
        return base

    _put.base = base
    return _put

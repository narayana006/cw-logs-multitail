"""Everything that talks to CloudWatch Logs, run against moto's in-memory mock."""

import io
import json

import boto3
import pytest
from botocore.stub import Stubber

from cw_logs_multitail.cli import main
from cw_logs_multitail.model import Source
from cw_logs_multitail.render import merge
from cw_logs_multitail.sources import fetch_events, resolve_groups
from cw_logs_multitail.tail import Reader, follow


def test_resolve_groups_by_name_and_pattern(aws):
    logs = aws.client("logs")
    for name in ("/aws/lambda/orders-api", "/aws/lambda/orders-worker", "/aws/lambda/billing", "/ecs/orders"):
        logs.create_log_group(logGroupName=name)
    assert resolve_groups(logs, ["/aws/lambda/orders-*"]) == ["/aws/lambda/orders-api", "/aws/lambda/orders-worker"]
    assert resolve_groups(logs, ["*orders*"]) == ["/aws/lambda/orders-api", "/aws/lambda/orders-worker", "/ecs/orders"]
    assert resolve_groups(logs, ["/aws/lambda/orders-?pi", "/ecs/orders", "/aws/lambda/orders-*"]) == \
        ["/aws/lambda/orders-api", "/ecs/orders", "/aws/lambda/orders-worker"]
    assert resolve_groups(logs, ["/aws/lambda/nothing-*"]) == []
    # a plain name is passed through without checking that it exists
    assert resolve_groups(logs, ["/not/created"]) == ["/not/created"]


def test_resolve_groups_follows_pagination():
    logs = boto3.client("logs", region_name="us-east-1")
    with Stubber(logs) as stub:
        stub.add_response("describe_log_groups", {"logGroups": [{"logGroupName": "/app/a"}], "nextToken": "t"},
                          {"logGroupNamePrefix": "/app/"})
        stub.add_response("describe_log_groups", {"logGroups": [{"logGroupName": "/app/b"}, {"logGroupName": "/app/x/y"}]},
                          {"logGroupNamePrefix": "/app/", "nextToken": "t"})
        assert resolve_groups(logs, ["/app/?"]) == ["/app/a", "/app/b"]
        stub.assert_no_pending_responses()


def test_fetch_events_time_range_and_service_side_filter(aws, put):
    logs = aws.client("logs")
    base = put(logs, "/app/api", [(0, "INFO start"), (1000, "ERROR boom"), (2000, "INFO end"), (3000, "ERROR late")])
    source = Source("/app/api", "us-east-1")
    assert [e.message for e in fetch_events(logs, source)] == ["INFO start", "ERROR boom", "INFO end", "ERROR late"]
    ranged = fetch_events(logs, source, base + 1000, base + 2000)
    assert [e.message for e in ranged] == ["ERROR boom", "INFO end"]
    assert ranged[0].stream == "s1" and ranged[0].source == source and ranged[0].event_id
    assert [e.message for e in fetch_events(logs, source, filter_pattern="ERROR")] == ["ERROR boom", "ERROR late"]


def test_reader_merges_groups_and_poll_returns_only_new_events(aws, put):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "api 1"), (200, "api 2")])
    put(logs, "/app/worker", [(100, "worker 1")])
    sources = [Source("/app/api", "us-east-1"), Source("/app/worker", "us-east-1")]
    reader = Reader({("", "us-east-1"): logs}, sources)
    start = put.base - 1000

    assert [e.message for e in merge(reader.read(start))] == ["api 1", "worker 1", "api 2"]
    assert [e.message for e in merge(reader.poll(start))] == []

    # a new event, plus a late one that lands before the newest event already shown
    put(logs, "/app/worker", [(400, "worker 2")], stream="s2")
    put(logs, "/app/api", [(50, "api late"), (500, "api 3")], stream="s2")
    assert [e.message for e in merge(reader.poll(start))] == ["api late", "worker 2", "api 3"]
    assert [e.message for e in merge(reader.poll(start))] == []


def test_reader_reports_a_missing_group_once_and_keeps_going(aws, put):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "ok")])
    errors = []
    sources = [Source("/app/api", "us-east-1"), Source("/app/missing", "us-east-1")]
    reader = Reader({("", "us-east-1"): logs}, sources, on_error=lambda s, exc: errors.append((s.group, str(exc))))
    assert [e.message for e in merge(reader.read(None))] == ["ok"]
    reader.poll(0)
    assert len(errors) == 1 and errors[0][0] == "/app/missing" and "ResourceNotFoundException" in errors[0][1]
    assert reader.failed == {sources[1]}


def test_follow_polls_at_the_interval_and_emits_new_events(aws, put):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "first")])
    reader = Reader({("", "us-east-1"): logs}, [Source("/app/api", "us-east-1")])
    reader.read(None)
    seen, sleeps = [], []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:  # something is logged while we wait for the second poll
            put(logs, "/app/api", [(900, "second")], stream="s2")

    follow(reader, 0, lambda batches: seen.append([e.message for e in merge(batches)]),
           interval=0.5, sleep=fake_sleep, max_polls=3)
    assert sleeps == [0.5, 0.5, 0.5]
    assert seen == [[], ["second"], []]


def factory(calls=None):
    def make(profile, region):
        session = boto3.Session(region_name=region or "us-east-1")
        if calls is not None:
            session.events.register("before-call", lambda model, **_: calls.append(model.name))
        return session
    return make


def run(argv, **kw):
    out = io.StringIO()
    code = main(argv, out=out, session_factory=kw.pop("session_factory", factory()), **kw)
    return code, out.getvalue().splitlines()


def test_cli_live_merge_filter_fields(aws, put, capsys):
    logs = aws.client("logs")
    put(logs, "/aws/lambda/orders-api", [
        (0, json.dumps({"level": "INFO", "msg": "order received", "requestId": "r1"})),
        (300, json.dumps({"level": "ERROR", "msg": "order failed", "requestId": "r1"}))])
    put(logs, "/aws/lambda/orders-worker", [(100, json.dumps({"level": "ERROR", "msg": "charge failed", "requestId": "r1"})),
                                            (200, "plain ERROR line")])
    put(logs, "/aws/lambda/billing", [(50, "ERROR unrelated group")])

    calls = []
    code, lines = run(["/aws/lambda/orders-*", "--since", "15m", "--filter", "ERROR", "--fields", "level,msg",
                       "--color", "never"], session_factory=factory(calls))
    assert code == 0
    assert [line.split("  ", 1)[1] for line in lines] == [
        'orders-worker  level=ERROR msg="charge failed"',
        "orders-worker  plain ERROR line",
        'orders-api     level=ERROR msg="order failed"',
    ]
    assert set(calls) == {"DescribeLogGroups", "FilterLogEvents"}  # the only two operations ever used

    code, lines = run(["/aws/lambda/orders-*", "--grep", r"charge|received", "--output", "jsonl"])
    assert [json.loads(line)["logGroup"] for line in lines] == ["/aws/lambda/orders-api", "/aws/lambda/orders-worker"]
    assert json.loads(lines[0])["region"] == "us-east-1"


def test_cli_since_and_until_bound_the_window(aws, put):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "old")])            # about 60 s ago
    put(logs, "/app/api", [(55_000, "recent")], stream="s2")  # about 5 s ago
    assert [l.rsplit("  ", 1)[1] for l in run(["/app/api", "--since", "30s"])[1]] == ["recent"]
    assert [l.rsplit("  ", 1)[1] for l in run(["/app/api", "--since", "5m", "--until", "30s"])[1]] == ["old"]


def test_cli_multiple_regions_are_labelled(aws, put):
    put(boto3.client("logs", region_name="us-east-1"), "/app/api", [(0, "from virginia")])
    put(boto3.client("logs", region_name="eu-west-1"), "/app/api", [(10, "from ireland")])
    code, lines = run(["/app/api", "--region", "us-east-1,eu-west-1"])
    assert code == 0
    assert [line.split("  ", 1)[1] for line in lines] == [
        "/app/api@us-east-1  from virginia", "/app/api@eu-west-1  from ireland"]


def test_cli_multiple_profiles_are_labelled(aws, put):
    put(aws.client("logs"), "/app/api", [(0, "hello")])
    asked = []

    def make(profile, region):
        asked.append(profile)
        return boto3.Session(region_name="us-east-1")  # both fake profiles see the same mock account

    code, lines = run(["/app/api", "--profile", "dev", "--profile", "prod"], session_factory=make)
    assert code == 0 and asked == ["dev", "prod"]
    assert [line.split("  ", 1)[1] for line in lines] == ["dev:/app/api   hello", "prod:/app/api  hello"]


def test_cli_follow_prints_backlog_then_new_events(aws, put):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "backlog")])

    def fake_sleep(_seconds):
        put(logs, "/app/api", [(30_000, "live")], stream="s2")

    code, lines = run(["/app/api", "--follow", "--interval", "0.1"], sleep=fake_sleep, max_polls=1)
    assert code == 0 and [l.rsplit("  ", 1)[1] for l in lines] == ["backlog", "live"]


def test_cli_exit_codes_for_missing_groups(aws, put, capsys):
    logs = aws.client("logs")
    put(logs, "/app/api", [(0, "ok")])
    assert run(["/nothing/here-*"])[0] == 2
    assert "no log groups match" in capsys.readouterr().err
    assert run(["/app/missing"])[0] == 1                      # every group failed
    assert "ResourceNotFoundException" in capsys.readouterr().err
    code, lines = run(["/app/missing", "/app/api"])           # partial failure still prints the rest
    assert code == 0 and len(lines) == 1

import io
import json
from datetime import datetime, timezone

import pytest

from cw_logs_multitail.localfilter import UnsupportedPattern, compile_pattern
from cw_logs_multitail.model import Event, Source, format_time, parse_time
from cw_logs_multitail.render import Printer, format_message, make_labels, merge, use_colour
from cw_logs_multitail.sources import load_file

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def test_parse_relative_and_absolute_times():
    assert parse_time("15m", NOW) == NOW_MS - 15 * 60_000
    assert parse_time("2h", NOW) == NOW_MS - 2 * 3_600_000
    assert parse_time("1d", NOW) == NOW_MS - 86_400_000
    assert parse_time("30s", NOW) == NOW_MS - 30_000
    assert parse_time("2026-03-01T09:30:00Z", NOW) == NOW_MS - 150 * 60_000
    assert parse_time("2026-03-01T10:30:00+01:00", NOW) == NOW_MS - 150 * 60_000
    assert parse_time("2026-03-01T09:30:00", NOW) == NOW_MS - 150 * 60_000  # naive means UTC
    assert parse_time("2026-03-01", NOW) == NOW_MS - 12 * 3_600_000


@pytest.mark.parametrize("bad", ["soon", "15", "m15", "15 minutes", ""])
def test_parse_time_rejects_garbage(bad):
    with pytest.raises(ValueError, match="cannot read time"):
        parse_time(bad, NOW)


def test_format_time_keeps_milliseconds():
    assert format_time(NOW_MS + 7) == "2026-03-01T12:00:00.007Z"


@pytest.mark.parametrize("pattern, message, expected", [
    ("", "anything", True),
    ("ERROR", "an ERROR happened", True),
    ("ERROR", "an error happened", False),               # case-sensitive
    ("ERROR timeout", "ERROR: upstream timeout", True),   # all terms
    ("ERROR timeout", "ERROR: refused", False),
    ("?ERROR ?WARN", "WARN disk", True),                  # any ?-term
    ("?ERROR ?WARN", "INFO fine", False),
    ("ERROR -healthcheck", "ERROR in healthcheck", False),
    ("ERROR -healthcheck", "ERROR in checkout", True),
    ('"connection reset"', "peer: connection reset by peer", True),
    ('"connection reset"', "connection was reset", False),
    ("db ?ERROR ?WARN", "WARN db slow", True),
    ("db ?ERROR ?WARN", "WARN cache slow", False),
])
def test_local_filter_terms(pattern, message, expected):
    assert compile_pattern(pattern)(message) is expected


@pytest.mark.parametrize("pattern", ['{ $.level = "ERROR" }', "[ip, user, status=5*]", "%ERR.*%", '"unclosed'])
def test_local_filter_rejects_what_it_cannot_do(pattern):
    with pytest.raises(UnsupportedPattern):
        compile_pattern(pattern)


def ev(ts, message, group="g", region="", profile=""):
    return Event(ts, message, Source(group, region, profile))


def test_merge_orders_by_time_and_is_stable_on_ties():
    a = [ev(1, "a1", "A"), ev(5, "a5", "A"), ev(5, "a5b", "A")]
    b = [ev(2, "b2", "B"), ev(5, "b5", "B")]
    c: list = []
    assert [e.message for e in merge([a, b, c])] == ["a1", "b2", "a5", "a5b", "b5"]
    assert list(merge([])) == []


def test_labels_strip_common_path_and_add_region_or_profile_only_when_needed():
    lam = [Source("/aws/lambda/orders-api"), Source("/aws/lambda/orders-worker")]
    assert list(make_labels(lam).values()) == ["orders-api", "orders-worker"]
    assert list(make_labels(lam, full_names=True).values()) == [s.group for s in lam]
    mixed = [Source("/aws/lambda/orders-api"), Source("/ecs/payments")]
    assert list(make_labels(mixed).values()) == ["/aws/lambda/orders-api", "/ecs/payments"]
    assert list(make_labels([Source("/aws/lambda/only")]).values()) == ["/aws/lambda/only"]
    # a shared name prefix that is not a whole path segment is kept
    assert list(make_labels([Source("/app/orders-a"), Source("/app/orders-b")]).values()) == ["orders-a", "orders-b"]
    regions = [Source("/app/api", "us-east-1"), Source("/app/api", "eu-west-1")]
    assert list(make_labels(regions).values()) == ["/app/api@us-east-1", "/app/api@eu-west-1"]
    profiles = [Source("/app/api", "us-east-1", "dev"), Source("/app/api", "us-east-1", "prod")]
    assert list(make_labels(profiles).values()) == ["dev:/app/api", "prod:/app/api"]


def test_format_message_fields():
    line = json.dumps({"level": "ERROR", "msg": "charge failed", "ctx": {"attempt": 3, "ok": False}, "empty": ""})
    assert format_message(line, ["level", "msg", "ctx.attempt", "ctx.ok", "missing"]) == \
        'level=ERROR msg="charge failed" ctx.attempt=3 ctx.ok=false'
    assert format_message(line, ["empty", "ctx"]) == 'empty="" ctx={"attempt":3,"ok":false}'
    assert format_message(line, ["nope"]) == line          # no requested field present: keep the line
    assert format_message(line, None) == line
    assert format_message("plain text\n", ["level"]) == "plain text"
    assert format_message("{not json", ["level"]) == "{not json"
    assert format_message("[1, 2]", ["level"]) == "[1, 2]"


def test_printer_text_colour_and_jsonl():
    sources = [Source("/aws/lambda/a", "us-east-1"), Source("/aws/lambda/bb", "us-east-1")]
    event = Event(NOW_MS, '{"level":"INFO"}\n', sources[0], stream="s1")
    plain = Printer(sources, io.StringIO()).line(event)
    assert plain == '2026-03-01T12:00:00.000Z  a   {"level":"INFO"}'
    coloured = Printer(sources, io.StringIO(), colour=True)
    assert coloured.line(event) == '2026-03-01T12:00:00.000Z  \033[36ma \033[0m  {"level":"INFO"}'
    assert "\033[33m" in coloured.line(Event(NOW_MS, "x", sources[1]))
    record = json.loads(Printer(sources, io.StringIO(), as_json=True).line(event))
    assert record == {"timestamp": NOW_MS, "logGroup": "/aws/lambda/a", "region": "us-east-1",
                      "logStreamName": "s1", "message": '{"level":"INFO"}'}


def test_use_colour(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert use_colour("auto", Tty()) and not use_colour("auto", io.StringIO())
    assert use_colour("always", io.StringIO()) and not use_colour("never", Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert not use_colour("auto", Tty())


def test_load_jsonl_and_cli_export_document(tmp_path):
    lines = tmp_path / "svc.jsonl"
    lines.write_text(
        '{"timestamp": 2000, "message": "second", "logGroup": "/g/a"}\n\n'
        '{"timestamp": 1000, "message": "first", "group": "/g/b", "logStreamName": "s"}\n'
        '{"timestamp": 3000.0, "message": "no group"}\n')
    events = load_file(str(lines))
    assert [(e.timestamp, e.source.group, e.stream) for e in events] == \
        [(1000, "/g/b", "s"), (2000, "/g/a", ""), (3000, "svc", "")]

    export = tmp_path / "orders-api.json"   # shape of `aws logs filter-log-events` output
    export.write_text(json.dumps({"events": [
        {"logStreamName": "s1", "timestamp": 5, "message": "hello", "ingestionTime": 6, "eventId": "e1"}],
        "searchedLogStreams": []}, indent=2))
    (event,) = load_file(str(export))
    assert (event.source.group, event.message, event.event_id) == ("orders-api", "hello", "e1")


@pytest.mark.parametrize("content, fragment", [
    ("not json\n", "not valid JSON"),
    ('{"message": "no timestamp"}\n', "each event needs"),
    ('{"timestamp": "yesterday", "message": "x"}\n', "must be a number"),
    ("[1, 2]\n", "each event needs"),
])
def test_load_file_errors_name_the_line(tmp_path, content, fragment):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(ValueError, match=fragment) as exc:
        load_file(str(path))
    assert "bad.jsonl" in str(exc.value)

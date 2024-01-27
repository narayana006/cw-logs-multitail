import io
import json
from pathlib import Path

import pytest

from cw_logs_multitail.cli import main

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FILES = [arg for name in ("orders-api", "orders-worker", "payments")
         for arg in ("--from-file", str(EXAMPLES / f"{name}.jsonl"))]


def boom(profile, region):
    raise AssertionError("--from-file must not create an AWS session")


def run(*argv):
    out = io.StringIO()
    code = main([*FILES, *argv], out=out, session_factory=boom)
    return code, out.getvalue().splitlines()


def test_example_files_merge_in_time_order():
    code, lines = run()
    assert code == 0 and len(lines) == 27  # 24 events, one is a 4-line traceback
    stamps = [l[:24] for l in lines if l.startswith("2026-")]
    assert len(stamps) == 24 and stamps == sorted(stamps)
    assert lines[0].startswith("2026-03-01T09:30:00.120Z  /aws/lambda/orders-api     START RequestId")
    assert "  /ecs/payments              " in lines[7]


def test_follow_one_request_across_services_with_fields():
    code, lines = run("--filter", "req-b81d04ee", "--fields", "level,msg")
    assert code == 0 and len(lines) == 10
    groups = [l.split("  ")[1].strip() for l in lines]
    assert groups[:5] == ["/aws/lambda/orders-api"] * 2 + ["/aws/lambda/orders-worker"] * 2 + ["/ecs/payments"]
    assert lines[-1].endswith('level=WARN msg="order marked failed"')


def test_group_pattern_since_until_and_grep():
    code, lines = run("/aws/lambda/orders-*", "--filter", "?ERROR ?WARN", "--since", "2026-03-01T09:30:04Z")
    assert [l.split("  ")[1].strip() for l in lines] == ["orders-worker", "orders-api", "orders-api"]
    code, lines = run("--until", "2026-03-01T09:30:00.200Z")
    assert len(lines) == 5
    code, lines = run("--grep", r"attempts?\":[23]")
    assert len(lines) == 3


def test_jsonl_output_round_trips(tmp_path):
    code, lines = run("--output", "jsonl", "--filter", "ERROR")
    assert code == 0 and all(json.loads(l)["logGroup"] for l in lines)
    saved = tmp_path / "errors.jsonl"
    saved.write_text("\n".join(lines) + "\n")
    out = io.StringIO()
    assert main(["--from-file", str(saved), "--output", "jsonl"], out=out, session_factory=boom) == 0
    assert out.getvalue().splitlines() == lines


def test_unsupported_offline_filter_and_bad_arguments(capsys):
    assert run("--filter", '{ $.level = "ERROR" }')[0] == 2
    assert "only plain-term filter patterns" in capsys.readouterr().err
    out = io.StringIO()
    assert main(["--from-file", "/no/such/file.jsonl"], out=out, session_factory=boom) == 2
    for argv in ([], ["--from-file", "x", "--follow"], ["--from-file", "x", "--region", "us-east-1"],
                 ["/g", "--follow", "--until", "5m"], ["/g", "--since", "whenever"],
                 ["/g", "--since", "5m", "--until", "10m"], ["/g", "--grep", "("], ["/g", "--interval", "0"]):
        with pytest.raises(SystemExit) as exc:
            main(argv, session_factory=boom)
        assert exc.value.code == 2

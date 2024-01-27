# cw-logs-multitail

Tail several CloudWatch Logs groups at once in one merged, colour-coded stream with filters.

**Status: working first version (0.1.0).** All five planned features are implemented: groups by name or pattern, a time-ordered merge with one colour per group, filter patterns and time ranges, field selection for JSON log lines, and several regions or profiles in one run. It also follows live (`--follow`) and can read exported log files offline. The AWS code paths are tested against [moto](https://github.com/getmoto/moto) (an in-memory AWS mock) and botocore stubs; they have not yet been run against a real AWS account by the author.

## Install

```bash
pip install git+https://github.com/narayana006/cw-logs-multitail
```

Python 3.10 or newer. The only runtime dependency is `boto3`.

## Try it without an AWS account

`examples/` holds three small exported log files (hand-written sample data: two Lambda functions and an ECS service handling the same orders). With `--from-file` nothing is sent to AWS and no credentials are needed. Following one request across the three services:

```console
$ cw-logs-multitail --from-file examples/orders-api.jsonl --from-file examples/orders-worker.jsonl \
    --from-file examples/payments.jsonl --filter req-b81d04ee --fields level,msg,reason,error,attempts
2026-03-01T09:30:02.040Z  /aws/lambda/orders-api     level=INFO msg="order received"
2026-03-01T09:30:02.101Z  /aws/lambda/orders-api     level=INFO msg="order queued"
2026-03-01T09:30:02.350Z  /aws/lambda/orders-worker  level=INFO msg="picked up order"
2026-03-01T09:30:02.395Z  /aws/lambda/orders-worker  level=INFO msg="calling payments"
2026-03-01T09:30:02.410Z  /ecs/payments              level=INFO msg="charge requested"
2026-03-01T09:30:02.990Z  /ecs/payments              level=WARN msg="card declined, retrying"
2026-03-01T09:30:03.720Z  /ecs/payments              level=WARN msg="card declined, retrying"
2026-03-01T09:30:04.470Z  /ecs/payments              level=ERROR msg="charge failed" error=card_declined attempts=3
2026-03-01T09:30:04.490Z  /aws/lambda/orders-worker  level=ERROR msg="payment failed, giving up" attempts=3
2026-03-01T09:30:04.875Z  /aws/lambda/orders-api     level=WARN msg="order marked failed" reason="payment declined"
```

That is the real output of the command above. In a terminal each group label has its own colour.

Naming groups narrows the set (the pattern also works on files), and when every selected group shares a path prefix it is dropped from the label:

```console
$ cw-logs-multitail '/aws/lambda/orders-*' --from-file examples/orders-api.jsonl \
    --from-file examples/orders-worker.jsonl --from-file examples/payments.jsonl \
    --since 2026-03-01T09:30:04Z --filter '?ERROR ?WARN'
2026-03-01T09:30:04.490Z  orders-worker  {"level":"ERROR","msg":"payment failed, giving up","requestId":"req-b81d04ee","orderId":"ord-1002","attempts":3}
2026-03-01T09:30:04.875Z  orders-api     {"level":"WARN","msg":"order marked failed","requestId":"req-b81d04ee","orderId":"ord-1002","reason":"payment declined"}
2026-03-01T09:30:06.055Z  orders-api     {"level":"ERROR","msg":"validation failed","requestId":"req-c55e7a10","orderId":"ord-1003","error":"missing shipping address"}
```

## Against CloudWatch Logs

```bash
# last 15 minutes of every matching group, errors only
cw-logs-multitail '/aws/lambda/orders-*' --since 15m --filter ERROR

# keep following, show three fields of each JSON line
cw-logs-multitail '/aws/lambda/orders-*' /ecs/payments --follow --fields level,msg,requestId

# a fixed window, two regions, a named profile
cw-logs-multitail /app/api --since 2026-03-01T09:00:00Z --until 2026-03-01T10:00:00Z \
    --region us-east-1,eu-west-1 --profile prod
```

Quote patterns so the shell does not expand them. Credentials come from the standard boto3 chain or `--profile`.

| Option | Meaning |
| --- | --- |
| `GROUP ...` | Log group names or shell-style patterns (`*`, `?`, `[abc]`). A plain name is used as is; a pattern is expanded with `DescribeLogGroups`. |
| `--since TIME` | Start of the window: `30s`, `15m`, `2h`, `1d`, `1w` ago, or an ISO-8601 timestamp (UTC if no offset). Default `10m`; with `--from-file`, no lower bound. |
| `--until TIME` | End of the window, same formats. Default: now. Not allowed with `--follow`. |
| `--filter PATTERN` | A [CloudWatch Logs filter pattern](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/FilterAndPatternSyntax.html), applied by the service. |
| `--grep REGEX` | Additional client-side regular expression (Python syntax) on the message. |
| `-f`, `--follow` | After the backlog, poll for new events every `--interval` seconds (default 2) until Ctrl-C. |
| `--fields A,B.C` | For lines that are a JSON object, print only these fields as `field=value`; dots reach nested keys. Other lines are printed unchanged, as are JSON lines containing none of the fields. |
| `--region R` | Region(s), repeated or comma-separated. Each pattern is resolved in each region. |
| `--profile P` | Profile(s), repeated or comma-separated, to read several accounts. |
| `--from-file FILE` | Read events from a file instead of AWS (repeatable). |
| `--output jsonl` | One JSON object per event instead of text. |
| `--color auto\|always\|never` | Colour for the group label. `auto` colours only a terminal and honours `NO_COLOR`. |
| `--full-names` | Do not shorten group names. |

Output lines are `<UTC timestamp>  <group>  <message>`. With more than one region the label becomes `group@region`; with more than one profile, `profile:group`.

Exit codes: `0` success, `1` every log group failed to read, `2` bad arguments, no matching groups, unreadable input or credential errors. If only some groups fail, a warning goes to stderr and the rest are still shown.

### Saving and replaying

`--output jsonl` writes events in the format `--from-file` reads, so a window can be captured once and re-filtered offline:

```console
$ cw-logs-multitail --from-file examples/payments.jsonl --grep declined --output jsonl
{"timestamp": 1772357402990, "logGroup": "/ecs/payments", "logStreamName": "payments/app/3f2e1d0c9b8a47f6a5b4c3d2e1f00a9b", "message": "{\"level\":\"WARN\",\"msg\":\"card declined, retrying\",\"requestId\":\"req-b81d04ee\",\"attempt\":1}"}
{"timestamp": 1772357403720, "logGroup": "/ecs/payments", "logStreamName": "payments/app/3f2e1d0c9b8a47f6a5b4c3d2e1f00a9b", "message": "{\"level\":\"WARN\",\"msg\":\"card declined, retrying\",\"requestId\":\"req-b81d04ee\",\"attempt\":2}"}
{"timestamp": 1772357404470, "logGroup": "/ecs/payments", "logStreamName": "payments/app/3f2e1d0c9b8a47f6a5b4c3d2e1f00a9b", "message": "{\"level\":\"ERROR\",\"msg\":\"charge failed\",\"requestId\":\"req-b81d04ee\",\"error\":\"card_declined\",\"attempts\":3}"}
```

`--from-file` accepts JSON Lines with `timestamp` (epoch milliseconds), `message` and optionally `logGroup`, `logStreamName`, `region`, or the JSON document printed by `aws logs filter-log-events` (events are then attributed to a group named after the file).

## IAM permissions

The tool is read-only and uses two actions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["logs:DescribeLogGroups", "logs:FilterLogEvents"],
      "Resource": "*"
    }
  ]
}
```

`logs:DescribeLogGroups` is only needed when you pass a pattern; plain names skip it. A test records every API call made during a run and asserts that these are the only two.

## How it works

1. Each pattern is expanded per region and profile. The literal text before the first wildcard is sent as `logGroupNamePrefix`, the rest is matched locally.
2. Groups are read in parallel (up to 8 at a time) with paginated `FilterLogEvents` calls.
3. The per-group results are merged by event timestamp. Events in the same millisecond keep the order in which the groups were listed.
4. With `--follow`, each poll re-reads the last 5 seconds before the newest event already shown and drops events it has printed (by event id), so events that become visible slightly late are still picked up.

## Features

- [x] Tail many log groups by name or pattern
- [x] Merged output in time order with a colour per group
- [x] Filter patterns and a time range
- [x] JSON log lines pretty-printed with chosen fields
- [x] Works across regions and profiles
- [x] Follow mode
- [x] Offline input and JSON Lines output
- [ ] Live streaming through the `StartLiveTail` API instead of polling
- [ ] Selecting or filtering by log stream name
- [ ] Local time zone display

## Limitations

- **Polling, not streaming.** `--follow` calls `FilterLogEvents` once per group per interval. Many groups with a short interval can hit the API rate limit; a throttled group is reported once on stderr and retried on the next poll.
- **Order in follow mode is per poll.** Each batch is sorted, but an event that arrives late is printed when it is seen, after newer lines from an earlier poll. Events that become visible more than 5 seconds behind the newest shown event of their group are missed in follow mode.
- **Large windows are read fully before printing.** The backlog of every group is held in memory to merge it, so a wide `--since` on busy groups is slow and memory-hungry. Narrow it with `--filter`.
- **`--filter` with `--from-file` supports plain terms only**: `ERROR timeout` (all terms), `?ERROR ?WARN` (any), `-term` (exclude), `"quoted phrase"`. JSON, space-delimited and regex patterns are rejected with an error in file mode; they work against the live service, which evaluates them itself.
- `--fields` only understands lines that are entirely a JSON object, not JSON embedded after a text prefix.
- Timestamps are shown in UTC only.

## Development

```bash
git clone https://github.com/narayana006/cw-logs-multitail
cd cw-logs-multitail
pip install -e ".[dev]"
pytest
```

The tests set fake credentials and run against moto and botocore's `Stubber`; they never contact AWS.

## Contributing

Issues and pull requests are welcome, especially reports from real accounts (throttling behaviour, ordering surprises) and log formats that `--fields` should handle.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)

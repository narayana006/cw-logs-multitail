# cw-logs-multitail

Tail several CloudWatch Logs groups at once in one merged, colour-coded stream with filters.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool.

**The problem it solves:** Following a request across services means opening several log groups in separate tabs.

## Who it is for

Developers and on-call engineers debugging on AWS.

## How it will work

1. You name the log groups or a pattern.
2. The tool reads them in parallel and merges the lines by timestamp.
3. It streams one readable feed to your terminal.

## Planned features

Unticked items are not built yet.

- [ ] Tail many log groups by name or pattern
- [ ] Merged output in time order with a colour per group
- [ ] Filter patterns and a time range
- [ ] JSON log lines pretty-printed with chosen fields
- [ ] Works across regions and profiles

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
cw-logs-multitail '/aws/lambda/orders-*' --since 15m --filter ERROR
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)

"""Client-side matcher for the plain-text subset of CloudWatch filter patterns.

Used only for --from-file input, where there is no service to do the filtering.
Supported, with the same meaning CloudWatch Logs gives them:

    ERROR timeout        every term must appear (case-sensitive)
    ?ERROR ?WARN         at least one of the ?-terms must appear
    ERROR -healthcheck   a term prefixed with - must not appear
    "connection reset"   quotes make a phrase with spaces one term

JSON ({ $.field = ... }), space-delimited ([a, b]) and regex (%...%) patterns
are not implemented here and are rejected rather than silently mis-applied.
"""

from __future__ import annotations

import shlex
from typing import Callable


class UnsupportedPattern(ValueError):
    pass


def compile_pattern(pattern: str) -> Callable[[str], bool]:
    pattern = pattern.strip()
    if not pattern:
        return lambda message: True
    if pattern[0] in "{[%":
        raise UnsupportedPattern(
            "only plain-term filter patterns work with --from-file "
            "(JSON, space-delimited and regex patterns need the live service); "
            "use --grep for a regular expression")
    try:
        lexer = shlex.shlex(pattern, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.quotes = '"'
        tokens = list(lexer)
    except ValueError as exc:
        raise UnsupportedPattern(f"cannot parse filter pattern: {exc}") from exc

    required, optional, excluded = [], [], []
    for token in tokens:
        if token.startswith("?") and len(token) > 1:
            optional.append(token[1:])
        elif token.startswith("-") and len(token) > 1:
            excluded.append(token[1:])
        else:
            required.append(token)

    def matches(message: str) -> bool:
        if any(term in message for term in excluded):
            return False
        if not all(term in message for term in required):
            return False
        return not optional or any(term in message for term in optional)

    return matches

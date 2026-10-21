"""Small text helpers for teacher-facing copy.

Counted nouns end up inside sentences ("1 student has not submitted",
"3 students have not submitted"), so the plural has to follow the number
instead of an "(s)" suffix the reader has to decode.
"""

from __future__ import annotations


def count(n: int, singular: str, plural: str | None = None) -> str:
    """``count(1, "student")`` -> ``"1 student"``; ``count(3, "student")`` -> ``"3 students"``."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def verb(n: int, singular: str, plural: str) -> str:
    """Agreement for the verb that follows a counted noun (``has``/``have``)."""
    return singular if n == 1 else plural

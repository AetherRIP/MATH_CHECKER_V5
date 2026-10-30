"""Provider usage metadata, extracted defensively.

Google returns token accounting on every ``generateContent`` response
(``usage_metadata``). It is the ONLY trustworthy source for "what did this
paper cost": counting pages or dividing a character count would be an
estimate presented as a measurement.

Two rules govern this module:

* **Never raise.** A response shape we do not recognise means "usage
  unavailable", not "the correction failed". A marking result must never be
  lost because a statistics field moved.
* **Never invent.** If the provider did not report a count, the field stays
  ``None`` and the caller records ``estimated = 0``. An estimate is only ever
  produced by :func:`estimate`, which marks itself as such so the dashboard can
  label it.

The SDK exposes both snake_case attributes (``prompt_token_count``) and the
camelCase JSON names (``promptTokenCount``); mock responses and tests may hand
over a plain dict. All three are accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

#: Attribute names that carry the same number, most specific first. Gemini has
#: renamed fields between generations (``response_token_count`` appears on
#: newer metadata, ``candidates_token_count`` on older), so both are read.
_FIELDS = {
    "input_tokens": ("prompt_token_count", "promptTokenCount"),
    "output_tokens": ("candidates_token_count", "candidatesTokenCount",
                      "response_token_count", "responseTokenCount"),
    "cached_tokens": ("cached_content_token_count", "cachedContentTokenCount"),
    "thoughts_tokens": ("thoughts_token_count", "thoughtsTokenCount"),
    "total_tokens": ("total_token_count", "totalTokenCount"),
}


def _as_int(value: object) -> int | None:
    """A non-negative int, or ``None`` when the value is not a count."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            number = int(value.strip())
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None
    return None


def _read(source: object, names: tuple[str, ...]) -> int | None:
    """First present, parseable count among ``names``."""
    for name in names:
        try:
            if isinstance(source, dict):
                if name in source:
                    parsed = _as_int(source.get(name))
                    if parsed is not None:
                        return parsed
            else:
                parsed = _as_int(getattr(source, name, None))
                if parsed is not None:
                    return parsed
        except Exception:  # noqa: BLE001 — a statistics field is never fatal
            continue
    return None


@dataclass(frozen=True)
class Usage:
    """Token accounting for one provider request."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    thoughts_tokens: int | None = None
    total_tokens: int | None = None
    estimated: bool = False

    @property
    def available(self) -> bool:
        """True when at least one real count was reported."""
        return any(value is not None for value in (
            self.input_tokens, self.output_tokens, self.cached_tokens,
            self.total_tokens))

    def to_dict(self) -> dict:
        return asdict(self)


def from_metadata(metadata: object) -> Usage | None:
    """Build a :class:`Usage` from ``usage_metadata`` (object or dict)."""
    if metadata is None:
        return None
    try:
        values = {field: _read(metadata, names) for field, names in _FIELDS.items()}
    except Exception:  # noqa: BLE001
        return None
    if not any(value is not None for value in values.values()):
        return None
    return Usage(estimated=False, **values)


def from_response(response: object) -> Usage | None:
    """Usage reported on a ``generateContent`` response, or ``None``.

    Accepts the SDK response object, an object carrying ``usage_metadata``, or
    a dict shaped like either. Mock responses have no metadata: that is
    "unavailable", never zero, so demo runs cannot pollute a token average.
    """
    if response is None:
        return None
    try:
        metadata = (response.get("usage_metadata")
                    if isinstance(response, dict)
                    else getattr(response, "usage_metadata", None))
    except Exception:  # noqa: BLE001
        return None
    if metadata is None:
        try:
            # Some callers hand over the metadata object itself.
            if not isinstance(response, dict):
                metadata = response
            else:
                metadata = response.get("usageMetadata")
        except Exception:  # noqa: BLE001
            return None
    return from_metadata(metadata)


def estimate(*, prompt_chars: int = 0, output_chars: int = 0,
             images: int = 0) -> Usage:
    """A labelled ESTIMATE, only for requests where nothing was reported.

    Deliberately crude (≈4 characters per token, 258 tokens per image tile
    unit) and deliberately flagged: the ledger stores ``estimated = 1`` so a
    report can separate measured from inferred numbers. Nothing in the
    correction path calls this automatically — an unavailable measurement is
    more useful than a plausible-looking guess.
    """
    text_tokens = max(0, int(prompt_chars)) // 4
    out_tokens = max(0, int(output_chars)) // 4
    image_tokens = max(0, int(images)) * 258
    return Usage(input_tokens=text_tokens + image_tokens,
                 output_tokens=out_tokens, cached_tokens=None,
                 thoughts_tokens=None,
                 total_tokens=text_tokens + image_tokens + out_tokens,
                 estimated=True)

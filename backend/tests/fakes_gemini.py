"""Scripted stand-ins for the Gemini client, shared by the offline tests.

A test that needs "the model answered X" should not have to re-implement the
response shape, and a test that needs "the answer was cut off" should be able to
say exactly that. These fakes give both, with no network and no keys: they mimic
the small slice of the SDK the app actually touches (``response.text``,
``response.candidates[0].finish_reason``, ``client.models.generate_content``,
``client.upload_file``).
"""

from __future__ import annotations

import json
from typing import Any, Callable


class Finish:
    def __init__(self, name: str) -> None:
        self.name = name


class Candidate:
    def __init__(self, name: str) -> None:
        self.finish_reason = Finish(name)


class Response:
    """The part of a generate_content response the app reads."""

    def __init__(self, text: str, finish: str = "STOP") -> None:
        self.text = text
        self.candidates = [Candidate(finish)]


class Remote:
    name = "files/fake-scheme"


class ScriptedModels:
    """Answers from a script (list) or from a responder callable."""

    def __init__(self, script: list[Any] | None = None,
                 responder: Callable[[Any, str, dict], Response] | None = None) -> None:
        self.script = list(script or [])
        self.responder = responder
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config=None, **kwargs):
        self.calls.append({"model": model, "contents": contents,
                           "config": config or {}})
        if self.responder is not None:
            return self.responder(contents, model, config or {})
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class FakeClient:
    """A client with a scripted model chain and a fake upload."""

    def __init__(self, script: list[Any] | None = None,
                 responder: Callable[[Any, str, dict], Response] | None = None) -> None:
        self.models = ScriptedModels(script, responder)

    def upload_file(self, path, *args, **kwargs):
        return Remote()

    def files_delete(self, remote, *args, **kwargs) -> None:
        return None


def payload(questions: list[dict], **extra) -> str:
    body: dict[str, Any] = {"questions": questions}
    body.update(extra)
    return json.dumps(body)

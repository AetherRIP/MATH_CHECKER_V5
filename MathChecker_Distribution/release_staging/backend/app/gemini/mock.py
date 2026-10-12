"""Offline mock of the Gemini surface for demo mode and smoke tests.

Mock mode produces a small but schema-faithful marking result so the full
pipeline (upload → mark → verify → save → display) can be exercised without
network access or a real API key. Results are clearly labelled as mock.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path


class MockState:
    name = "ACTIVE"


class MockRemoteFile:
    def __init__(self, name: str):
        self.name = name
        self.state = MockState()


class MockFile:
    """Stands in for a Gemini remote file."""

    def __init__(self, path: str):
        self.name = f"files/mock-{Path(path).stem}-{random.randint(1000, 9999)}"
        self.state = MockState()


class MockClientHolder:
    def files_get(self, remote):
        return remote

    def files_delete(self, remote):
        pass


class MockResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)


class MockModels:
    """generate_content that returns a deterministic small marking result."""

    def generate_content(self, *, model: str, contents, config=None, **kwargs):
        time.sleep(0.2)  # simulate a small latency
        questions = [
            {
                "question_number": "1",
                "max_marks": 3,
                "awarded_marks": 2,
                "topic": "Algebra — expanding brackets",
                "student_answer": "(x+3)(x-2) = x^2 - x - 5",
                "working_summary": "Expanded double brackets; final constant wrong.",
                "loss_reason": "Final constant is -5 instead of -6 (sign slip).",
                "mark_breakdown": [
                    {"code": "M1", "awarded": True, "reason": "Correct expansion method seen."},
                    {"code": "A1", "awarded": True, "reason": "x^2 - x correct."},
                    {"code": "A1", "awarded": False, "reason": "Constant term incorrect."},
                ],
                "correct_method": "(x+3)(x-2) = x^2 + x - 6",
                "full_mark_solution": "x² + 3x - 2x - 6 = x² + x - 6",
            },
            {
                "question_number": "2",
                "max_marks": 4,
                "awarded_marks": 4,
                "topic": "Geometry — angles in polygons",
                "student_answer": "Interior angle = 150°, sum = 1440°, decagon.",
                "working_summary": "All steps correct with reasons.",
                "loss_reason": "",
                "mark_breakdown": [
                    {"code": "M1", "awarded": True, "reason": "Used (n-2)×180."},
                    {"code": "A1", "awarded": True, "reason": "1440° correct."},
                    {"code": "M1", "awarded": True, "reason": "Divided by n."},
                    {"code": "A1", "awarded": True, "reason": "150° correct."},
                ],
                "correct_method": "(10-2)×180 = 1440; 1440/10 = 150",
                "full_mark_solution": "Sum = 1440°, each interior angle = 150°.",
            },
            {
                "question_number": "3",
                "max_marks": 5,
                "awarded_marks": 3,
                "topic": "Trigonometry — right-angled triangles",
                "student_answer": "sin x = 7/12, x = 35.7°",
                "working_summary": "Chose the correct ratio but rounded early.",
                "loss_reason": "Premature rounding lost the A marks for accuracy.",
                "mark_breakdown": [
                    {"code": "M1", "awarded": True, "reason": "Correct ratio chosen."},
                    {"code": "M1", "awarded": True, "reason": "Substitution correct."},
                    {"code": "A1", "awarded": False, "reason": "Rounded before final line."},
                    {"code": "A1", "awarded": True, "reason": "35.7° seen."},
                    {"code": "B1", "awarded": False, "reason": "Units missing."},
                ],
                "correct_method": "x = arcsin(7/12) = 35.69° ≈ 35.7°",
                "full_mark_solution": "sin x = 7/12 → x = 35.7° (1 d.p.)",
            },
        ]
        payload = {
            "paper_info": {
                "qualification": "Pearson Edexcel International GCSE Mathematics",
                "paper": "Mock Paper 1H (demo)",
                "session": "Mock session",
                "max_marks": 12,
            },
            "questions": questions,
            "overall_notes": {
                "summary": "Solid grasp of method; accuracy slips under pressure (demo mock result).",
                "strengths": ["Polygon angle reasoning", "Selecting correct trig ratio"],
                "revision_areas": ["Sign care when expanding brackets", "Rounding only at the final step"],
            },
            "uncertainties": ["Question 3 handwriting partly unclear (mock)."],
        }
        return MockResponse(payload)


class MockGeminiClient:
    """Full client shape: .models + .files, usable by the whole pipeline."""

    def __init__(self):
        self.models = MockModels()
        self.files = MockFiles()

    def upload(self, file: str):
        return MockFile(file)


class MockFiles(MockClientHolder):
    def upload(self, file: str):
        return MockFile(file)

    def get(self, *, name: str):
        return MockRemoteFile(name)

    def delete(self, *, name: str):
        return None

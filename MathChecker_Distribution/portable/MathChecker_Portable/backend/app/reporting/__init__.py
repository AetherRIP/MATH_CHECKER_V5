"""Report rendering: the PDF design system and the reports built on it.

``design`` is the single source of visual truth. Report generators compose
components from it instead of hand-rolling table styles, so every PDF the app
exports shares one identity, one set of measurements and one set of rules about
what is safe to print.
"""

from __future__ import annotations

__all__ = ["design"]

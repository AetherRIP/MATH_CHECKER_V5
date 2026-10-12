"""A shipped build must not carry one person's identity (§14, §20).

These tests lock the letterhead defaults: a fresh install starts with an EMPTY
branding record, the personal logo/signature files that live beside the
reporting code are never used unless the config opts in, and the release
allowlist refuses to ship those files at all.

The old behaviour - "Mr. Abanoub Akram" / "HLC Center — O Level" / a personal
tagline and monogram as defaults, plus a bundled signature - put the build
owner's identity on every other teacher's reports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def test_fresh_config_has_no_personal_branding(fresh_state):
    from app.config import get_config

    cfg = get_config()
    assert cfg.teacher.teacher_name == ""
    assert cfg.reports.teacher_title == ""
    assert cfg.reports.center_name == ""
    assert cfg.reports.academic_year == ""
    assert cfg.reports.quote == ""
    assert cfg.reports.monogram == ""
    assert cfg.reports.logo_path == ""
    assert cfg.reports.signature_path == ""
    assert cfg.reports.use_bundled_assets is False


def test_bundled_personal_assets_are_opt_in_only(fresh_state):
    """The dev tree's logo/signature must not reach a report by default."""
    from app.config import get_config
    from app.reporting import design as D

    # The files are present in the private development tree...
    assert D._bundled_asset("logo.png")
    assert D._bundled_asset("signature.png")

    # ...but a default install never uses them.
    brand = D.load_branding()
    assert brand.logo_path == ""
    assert brand.signature_path == ""

    # An explicit opt-in (private tree only) still works.
    cfg = get_config()
    cfg.reports.use_bundled_assets = True
    opted = D.load_branding()
    assert opted.logo_path.endswith("logo.png")
    assert opted.signature_path.endswith("signature.png")


def test_monogram_falls_back_to_the_apps_own_mark(fresh_state):
    """No logo, no configured monogram, no teacher name -> neutral mark."""
    from app.reporting import design as D

    brand = D.load_branding()
    assert brand.monogram == ""
    mark = D.Monogram(brand.monogram, D.make_theme())
    assert mark.letters == "M"  # the design system's own mark, not a person's


@pytest.mark.parametrize("name", ["logo.png", "signature.png"])
def test_release_allowlist_never_ships_the_personal_asset(name):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "MathChecker_Distribution" / "scripts"))
    try:
        import release_allowlist
    finally:
        sys.path.pop(0)
    assert release_allowlist.is_allowed(f"backend/app/reporting/assets/{name}") is False


def test_teacher_supplied_signature_is_used(fresh_state, tmp_path):
    """A teacher CAN set their own signature - the route/settings key exists."""
    from app.config import get_config
    from app.reporting import design as D

    own = tmp_path / "my-signature.png"
    own.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    cfg = get_config()
    cfg.reports.signature_path = str(own)
    cfg.reports.logo_path = str(own)
    brand = D.load_branding()
    assert brand.signature_path == str(own)
    assert brand.logo_path == str(own)

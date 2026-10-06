from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def zones_dir(fixtures: Path) -> Path:
    return fixtures / "zones"


@pytest.fixture(scope="session")
def checkconf_text(fixtures: Path) -> str:
    return (fixtures / "named-checkconf-p.txt").read_text(encoding="utf-8")

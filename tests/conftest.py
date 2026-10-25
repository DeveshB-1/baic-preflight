import json
from pathlib import Path

import pytest

from baic_preflight.guide import load_spec

FIX = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def spec():
    return load_spec()


@pytest.fixture
def fixture():
    return lambda name: json.loads((FIX / name).read_text())

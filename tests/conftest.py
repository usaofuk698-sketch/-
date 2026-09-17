import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goldbot.data.synthetic import generate  # noqa: E402


@pytest.fixture(scope="session")
def bars() -> pd.DataFrame:
    """A small, deterministic slice of synthetic M5 gold."""
    return generate(start="2023-01-02", end="2023-05-01", seed=5)


@pytest.fixture(scope="session")
def long_bars() -> pd.DataFrame:
    return generate(start="2022-01-03", end="2024-01-01", seed=5)

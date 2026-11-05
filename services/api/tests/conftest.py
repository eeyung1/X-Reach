import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def isolate_api_database(tmp_path, monkeypatch):
    monkeypatch.setenv("XREACH_DATABASE_PATH", str(tmp_path / "api.sqlite3"))

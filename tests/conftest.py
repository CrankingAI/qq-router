import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch, tmp_path):
    """Keep the developer's own ~/.config/qq/config.toml out of the tests.

    Without this the suite reads whatever the machine running it is configured
    to do, so a real config with ``search = true`` in it fails a dozen tests
    that have nothing to do with search. CI passes because CI has no config
    file, which is exactly the kind of green that hides a bug.

    Tests that want a config file still set ``QQ_CONFIG_DIR`` themselves; a
    later ``monkeypatch.setenv`` wins over this one.
    """
    monkeypatch.setenv("QQ_CONFIG_DIR", str(tmp_path / "empty-config"))

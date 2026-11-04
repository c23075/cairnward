"""Tests must never reach the real backup folder. With no `repository` set, backups use
~/cairnward-backups, and a real one there once had a test run restic against it."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from cairnward import backup


@pytest.fixture(scope="session", autouse=True)
def no_real_backup_folder(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    real_home = Path.home()
    real_default = backup.default_folder
    stand_in = tmp_path_factory.mktemp("default-backups")

    def default_folder() -> Path:  # a test that sets its own HOME gets the real logic
        return stand_in if Path.home() == real_home else real_default()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(backup, "default_folder", default_folder)
        yield

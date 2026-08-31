from __future__ import annotations

import os
from pathlib import Path

import pytest

from utils import durable_file


def test_atomic_write_text_fsyncs_file_and_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "journal.json"
    expected_fsync_calls = 2
    real_fsync = os.fsync
    fsynced_descriptors: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_descriptors.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(durable_file.os, "fsync", recording_fsync)

    durable_file.atomic_write_text(destination, '{"durable":true}')

    assert destination.read_text(encoding="utf-8") == '{"durable":true}'
    assert len(fsynced_descriptors) == expected_fsync_calls
    assert list(tmp_path.glob(".journal.json.*.tmp")) == []

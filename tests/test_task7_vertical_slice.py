import pytest

from scripts import task7_vertical_slice as task7


def test_evidence_run_directory_is_confined_and_not_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(task7, "EVIDENCE_ROOT", tmp_path)

    created = task7._prepare_evidence_dir("task7-run-1", fresh=True)

    assert created == tmp_path / "task7-run-1"
    with pytest.raises(FileExistsError):
        task7._prepare_evidence_dir("task7-run-1", fresh=True)
    assert task7._prepare_evidence_dir("task7-run-1", fresh=False) == created
    with pytest.raises(ValueError):
        task7._evidence_path(tmp_path, "../existing-run")

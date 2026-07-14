import pytest

from council_of_agents.scripts.artifact_store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


def test_content_addressed_write_is_deduplicated(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_text("same", provenance={"source": "one"})
    second = store.put_text("same", provenance={"source": "two"})

    assert first.id == second.id
    assert first.sha256 == second.sha256
    assert store.read(first) == b"same"
    assert len(list((tmp_path / "artifacts").rglob("*"))) == 2  # prefix dir + blob


def test_large_artifact_is_bounded_with_original_provenance(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024)
    artifact = store.put_bytes(b"a" * 5000)
    captured = store.read(artifact)

    assert len(captured) == 1024
    assert b"artifact truncated by policy" in captured
    assert artifact.provenance["truncated"] is True
    assert artifact.provenance["original_size_bytes"] == 5000


def test_read_detects_blob_corruption(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_text("trusted")
    store._path_for_hash(artifact.sha256).write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        store.read(artifact)


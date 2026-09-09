"""Load and save artifacts as human-readable JSON, with path-safety guarantees."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from app.artifacts.schema import CapabilityArtifact

ARTIFACT_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*\.json$")


class ArtifactLoadError(ValueError):
    """The file is not a valid artifact (malformed JSON or schema violation)."""


class UnsafeArtifactPathError(ValueError):
    """The requested filename would escape the artifacts directory or is not a plain name."""


def artifact_filename(artifact: CapabilityArtifact) -> str:
    return f"{artifact.name}.v{artifact.version}.json"


def safe_artifact_path(artifacts_dir: Path, filename: str) -> Path:
    """Resolve ``filename`` inside ``artifacts_dir`` and refuse traversal or odd names."""
    if not ARTIFACT_FILENAME_RE.match(filename):
        raise UnsafeArtifactPathError(f"artifact filename {filename!r} is not allowed")
    root = artifacts_dir.resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root:
        raise UnsafeArtifactPathError(f"artifact path {filename!r} escapes {root}")
    return candidate


def save_artifact(
    artifact: CapabilityArtifact, artifacts_dir: Path, filename: str | None = None
) -> Path:
    path = safe_artifact_path(artifacts_dir, filename or artifact_filename(artifact))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(artifact), encoding="utf-8")
    return path


def to_json(artifact: CapabilityArtifact) -> str:
    return json.dumps(artifact.model_dump(mode="json", exclude_none=True), indent=2) + "\n"


def load_artifact(path: Path) -> CapabilityArtifact:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactLoadError(f"cannot read artifact {path}: {exc}") from exc
    try:
        return CapabilityArtifact.model_validate(raw)
    except ValidationError as exc:
        raise ArtifactLoadError(f"artifact {path} is invalid:\n{exc}") from exc

"""Evidence storage with deterministic, traversal-safe filenames.

Layout: ``<evidence_root>/<kind>/<run_id>/<name>``. Text and JSON evidence pass through the run's
redactor; screenshots are produced with password fields masked by the surface.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.safety.redaction import Redactor

EvidenceKind = Literal["discovery", "replay", "failure"]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UnsafeEvidenceNameError(ValueError):
    pass


def new_run_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(2)}"


def run_directory(evidence_root: Path, kind: EvidenceKind, run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise UnsafeEvidenceNameError(f"run id {run_id!r} is not allowed")
    root = evidence_root.resolve()
    path = (root / kind / run_id).resolve()
    if root not in path.parents:
        raise UnsafeEvidenceNameError(f"run directory escapes {root}")
    return path


class EvidenceStore:
    def __init__(self, run_dir: Path, redactor: Redactor) -> None:
        self.run_dir = run_dir
        self._redactor = redactor
        self.refs: list[str] = []
        run_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not _NAME_RE.match(name):
            raise UnsafeEvidenceNameError(f"evidence name {name!r} is not allowed")
        return self.run_dir / name

    def _record(self, path: Path) -> str:
        ref = str(path.relative_to(self.run_dir))
        if ref not in self.refs:
            self.refs.append(ref)
        return ref

    def save_png(self, name: str, data: bytes) -> str:
        path = self._path(name if name.endswith(".png") else f"{name}.png")
        path.write_bytes(data)
        return self._record(path)

    def save_json(self, name: str, payload: Any) -> str:
        path = self._path(name if name.endswith(".json") else f"{name}.json")
        path.write_text(
            json.dumps(self._redactor.redact(payload), indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        return self._record(path)

    def save_text(self, name: str, text: str) -> str:
        path = self._path(name if name.endswith(".txt") else f"{name}.txt")
        path.write_text(self._redactor.redact_text(text) + "\n", encoding="utf-8")
        return self._record(path)

    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @staticmethod
    def step_file_name(index: int, step_id: str, suffix: str) -> str:
        safe_step = re.sub(r"[^a-z0-9_-]", "-", step_id.lower())
        return f"step-{index:02d}-{safe_step}-{suffix}"

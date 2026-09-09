"""Application profiles: curated, per-vendor knowledge embedded into artifacts at record time.

A profile carries the runtime-condition rules (business outcomes, known interstitials, fatal
states) for one vendor product. Discovery attaches them to every artifact recorded against
that product; tenant-specific overrides would layer on top (see REPORT.md section 4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.artifacts.schema import ConditionRule, StrictModel


class ProfileLoadError(ValueError):
    pass


class ApplicationProfile(StrictModel):
    vendor: str
    product: str
    surface: Literal["web"] = "web"
    application_version: str = Field(description="Version the profile was authored against")
    supported_versions: list[str] = Field(min_length=1)
    description: str = ""
    conditions: list[ConditionRule] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> ApplicationProfile:
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ProfileLoadError(f"cannot load application profile {path}: {exc}") from exc

    def status_values(self) -> list[str]:
        """Every business-outcome status this profile can report, plus ``success``."""
        values = ["success"]
        for rule in self.conditions:
            status = rule.outputs.get("status")
            if status and status not in values:
                values.append(status)
        return values

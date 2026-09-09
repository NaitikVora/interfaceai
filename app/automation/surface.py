"""The computer-use surface abstraction.

A ``ComputerSurface`` is anything that can be observed and acted upon: a browser page today, an
accessibility tree or a desktop window tomorrow. The artifact never references a surface
implementation; it references ``TargetSpec`` strategies that a ``StrategyBackend`` knows how to
evaluate on its surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.artifacts.schema import Strategy, TargetSpec, Viewport


@dataclass(frozen=True)
class ObservationLimits:
    """Bounds that keep an observation token-conscious regardless of page size."""

    max_text_chars: int
    max_controls: int
    max_tables: int
    max_table_rows: int
    max_table_cols: int
    max_cell_chars: int


class ContainerDescriptor(BaseModel):
    """Nearest semantic ancestor of a control (form, landmark, classed table/cell)."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    attributes: dict[str, str] = Field(default_factory=dict)
    css: str

    def to_target_spec(self) -> TargetSpec:
        return TargetSpec(
            description=f"{self.tag} container",
            attributes={"tag": self.tag, **self.attributes},
            css=self.css,
        )


class ControlDescriptor(BaseModel):
    """One interactive element as perceived on the surface.

    ``to_target_spec`` turns it into a multi-strategy artifact target. Recorders should verify
    each strategy against the live surface before persisting it (see ``recorder.py``).
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(description="Short id valid for this observation only, e.g. c3")
    tag: str
    role: str
    name: str = Field(default="", description="Computed accessible name")
    label: str | None = None
    text: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    visible: bool = True
    checked: bool | None = None
    options: list[str] | None = None
    container: ContainerDescriptor | None = None
    css: str
    xpath: str
    bbox: tuple[float, float, float, float] = Field(description="x, y, width, height")

    def to_target_spec(self, viewport: Viewport | None = None) -> TargetSpec:
        attrs = {
            k: v
            for k, v in self.attributes.items()
            if k in {"name", "id", "type", "value", "title", "href", "placeholder"} and v
        }
        attrs = {"tag": self.tag, **attrs}
        spec = TargetSpec(
            description=self.describe(),
            role=self.role or None,
            name=self.name or None,
            label=self.label,
            attributes=attrs,
            text=self.text if self.tag in {"a", "button", "td", "th", "span", "div"} else None,
            css=self.css,
            xpath=self.xpath,
        )
        if viewport is not None:
            x, y, w, h = self.bbox
            spec = spec.model_copy(
                update={
                    "coordinates": {"x": round(x + w / 2, 1), "y": round(y + h / 2, 1)},
                    "viewport": viewport,
                }
            )
        return spec

    def describe(self) -> str:
        role = self.role or self.tag
        if self.name:
            return f"{role} '{self.name}'"
        if self.label:
            return f"{role} labelled '{self.label}'"
        if name := self.attributes.get("name"):
            return f"{role} [name={name}]"
        return f"{role} ({self.css})"


class TableDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    css: str


class Observation(BaseModel):
    """Bounded, structured view of the surface at one instant."""

    model_config = ConfigDict(extra="forbid")

    url: str
    title: str
    headings: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list, description="Alerts/notices/errors seen")
    dialogs: list[str] = Field(default_factory=list, description="Native dialogs auto-dismissed")
    text: str = ""
    text_truncated: bool = False
    controls: list[ControlDescriptor] = Field(default_factory=list)
    controls_truncated: bool = False
    tables: list[TableDescriptor] = Field(default_factory=list)
    captured_at: datetime

    def control(self, ref: str) -> ControlDescriptor | None:
        return next((c for c in self.controls if c.ref == ref), None)

    def table(self, ref: str) -> TableDescriptor | None:
        return next((t for t in self.tables if t.ref == ref), None)

    def fingerprint(self) -> str:
        """Coarse identity of the page state, used for stuck detection."""
        return f"{self.url}|{'/'.join(self.headings)}|{len(self.controls)}|{self.text[:200]}"


@dataclass(frozen=True)
class MatchedElement:
    """An element matched by one strategy. ``handle`` is opaque to everything but the surface."""

    handle: Any
    visible: bool
    enabled: bool


class StrategyBackend(Protocol):
    """Evaluates one locator strategy of a ``TargetSpec`` on a concrete surface.

    ``scope`` restricts semantic strategies to descendants of a previously matched container.
    """

    async def match(
        self, spec: TargetSpec, strategy: Strategy, scope: MatchedElement | None = None
    ) -> list[MatchedElement]: ...


class ComputerSurface(Protocol):
    """Observe and act. Pausing/resuming is a session-ownership concern (``OwnedSurface``)."""

    @property
    def backend(self) -> StrategyBackend: ...

    @property
    def viewport(self) -> Viewport: ...

    async def observe(self, limits: ObservationLimits) -> Observation: ...

    async def navigate(self, url: str) -> None: ...

    async def click(self, element: MatchedElement) -> None: ...

    async def click_at(self, x: float, y: float) -> None: ...

    async def type_text(self, element: MatchedElement, value: str, *, clear: bool) -> None: ...

    async def select_option(self, element: MatchedElement, value: str) -> None: ...

    async def press(self, key: str) -> None: ...

    async def read_text(self, element: MatchedElement) -> str: ...

    async def screenshot(self) -> bytes: ...

    async def current_url(self) -> str: ...

    async def page_text(self) -> str: ...

    async def page_headings(self) -> list[str]: ...

    async def wait_for_settled(self, timeout_s: float) -> None: ...

"""In-memory surface and strategy backend for unit tests (no browser)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.artifacts.schema import Strategy, TargetSpec, Viewport
from app.automation.surface import MatchedElement, Observation, ObservationLimits


@dataclass
class FakeBackend:
    """Maps (strategy, key) -> matched elements. Key is the spec's distinguishing value."""

    matches: dict[tuple[Strategy, str], list[MatchedElement]] = field(default_factory=dict)
    calls: list[tuple[Strategy, str, bool]] = field(default_factory=list)

    @staticmethod
    def key_for(spec: TargetSpec, strategy: Strategy) -> str:
        match strategy:
            case Strategy.ROLE_NAME:
                return f"{spec.role}:{spec.name}"
            case Strategy.LABEL:
                return spec.label or ""
            case Strategy.ATTRIBUTES:
                return ",".join(f"{k}={v}" for k, v in sorted((spec.attributes or {}).items()))
            case Strategy.TEXT:
                return spec.text or ""
            case Strategy.TABLE_CELL:
                return spec.table_cell.row_match if spec.table_cell else ""
            case Strategy.CSS:
                return spec.css or ""
            case Strategy.XPATH:
                return spec.xpath or ""
            case Strategy.COORDINATES:
                return "coords"
        raise ValueError(strategy)

    async def match(
        self, spec: TargetSpec, strategy: Strategy, scope: MatchedElement | None = None
    ) -> list[MatchedElement]:
        key = self.key_for(spec, strategy)
        self.calls.append((strategy, key, scope is not None))
        scoped_key = (strategy, f"scoped:{key}")
        if scope is not None and scoped_key in self.matches:
            return self.matches[scoped_key]
        return self.matches.get((strategy, key), [])


def element(name: str, *, visible: bool = True, enabled: bool = True) -> MatchedElement:
    return MatchedElement(handle=name, visible=visible, enabled=enabled)


@dataclass
class FakeSurface:
    """A surface whose state is set directly by the test."""

    url: str = "http://localhost:8000/home"
    text: str = ""
    headings: list[str] = field(default_factory=list)
    backend_impl: FakeBackend = field(default_factory=FakeBackend)
    values: dict[str, str] = field(default_factory=dict)
    actions: list[tuple[str, object]] = field(default_factory=list)
    screenshots: int = 0

    @property
    def backend(self) -> FakeBackend:
        return self.backend_impl

    @property
    def viewport(self) -> Viewport:
        return Viewport(width=1280, height=900)

    async def observe(self, limits: ObservationLimits) -> Observation:
        return Observation(
            url=self.url,
            title="fake",
            headings=list(self.headings),
            text=self.text[: limits.max_text_chars],
            captured_at=datetime.now(UTC),
        )

    async def navigate(self, url: str) -> None:
        self.actions.append(("navigate", url))
        self.url = url

    async def click(self, element: MatchedElement) -> None:
        self.actions.append(("click", element.handle))

    async def click_at(self, x: float, y: float) -> None:
        self.actions.append(("click_at", (x, y)))

    async def type_text(self, element: MatchedElement, value: str, *, clear: bool) -> None:
        self.actions.append(("type", (element.handle, value)))
        self.values[str(element.handle)] = value

    async def select_option(self, element: MatchedElement, value: str) -> None:
        self.actions.append(("select", (element.handle, value)))

    async def press(self, key: str) -> None:
        self.actions.append(("press", key))

    async def read_text(self, element: MatchedElement) -> str:
        return self.values.get(str(element.handle), str(element.handle))

    async def screenshot(self) -> bytes:
        self.screenshots += 1
        return b"\x89PNG fake"

    async def current_url(self) -> str:
        return self.url

    async def page_text(self) -> str:
        return self.text

    async def page_headings(self) -> list[str]:
        return list(self.headings)

    async def wait_for_settled(self, timeout_s: float) -> None:
        return None

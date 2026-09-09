"""Locator resolution: turn a ``TargetSpec`` into exactly one element, or fail explicitly.

Rules:

* Strategies are tried strictly in ``STRATEGY_PRIORITY`` order (semantic before structural).
* A strategy that matches several usable elements is *never* used. A more specific *semantic*
  strategy further down the chain may still disambiguate.
* Structural strategies (css/xpath) recover from **drift** (every semantic strategy matched
  nothing) but never break a **semantic tie**: a positional selector cannot know which of two
  identical "Search" buttons is the right one. Ties resolve to AMBIGUOUS_TARGET.
* ``within`` scopes semantic strategies to a container that must itself resolve uniquely.
* Fallback specs (alternative controls) are tried only after the primary spec is exhausted.
* Coordinates are not an element strategy; the caller opts in and they are offered only when
  every element strategy found nothing.
* Every attempt is recorded so failure evidence explains exactly what was tried.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.artifacts.schema import (
    SEMANTIC_STRATEGIES,
    STRUCTURAL_STRATEGIES,
    Point,
    Strategy,
    TargetSpec,
)
from app.automation.surface import MatchedElement, StrategyBackend
from app.automation.waits import Deadline


class ResolutionFailure(StrEnum):
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"


@dataclass(frozen=True)
class StrategyAttempt:
    spec_index: int
    strategy: Strategy
    matches: int
    usable: int
    detail: str


@dataclass
class ResolutionDiagnostics:
    attempts: list[StrategyAttempt] = field(default_factory=list)
    polls: int = 0
    duration_ms: int = 0
    drift_fallback: bool = False
    """True when a structural strategy was used because semantic strategies matched nothing."""

    def as_dict(self) -> dict[str, object]:
        return {
            "polls": self.polls,
            "duration_ms": self.duration_ms,
            "drift_fallback": self.drift_fallback,
            "attempts": [
                {
                    "spec": a.spec_index,
                    "strategy": a.strategy.value,
                    "matches": a.matches,
                    "usable": a.usable,
                    "detail": a.detail,
                }
                for a in self.attempts
            ],
        }


@dataclass(frozen=True)
class ResolvedTarget:
    spec: TargetSpec
    strategy: Strategy
    element: MatchedElement | None
    point: Point | None
    diagnostics: ResolutionDiagnostics


class ResolutionError(Exception):
    def __init__(
        self, code: ResolutionFailure, spec: TargetSpec, diagnostics: ResolutionDiagnostics
    ) -> None:
        self.code = code
        self.spec = spec
        self.diagnostics = diagnostics
        super().__init__(f"{code}: {spec.summary()}")


@dataclass
class _Pass:
    resolved: ResolvedTarget | None = None
    ambiguous: bool = False


class LocatorResolver:
    def __init__(
        self,
        backend: StrategyBackend,
        *,
        poll_interval_s: float,
        ambiguity_confirmations: int = 2,
    ) -> None:
        self._backend = backend
        self._poll_interval_s = poll_interval_s
        self._ambiguity_confirmations = ambiguity_confirmations

    async def resolve(
        self,
        spec: TargetSpec,
        *,
        timeout_s: float,
        require_enabled: bool = True,
        allow_coordinates: bool = False,
    ) -> ResolvedTarget:
        """Resolve within ``timeout_s``; polls because legacy pages render late.

        Ambiguity is confirmed on consecutive polls and then fails fast: waiting longer does
        not make two identical buttons distinguishable.
        """
        started = time.monotonic()
        deadline = Deadline(timeout_s)
        diagnostics = ResolutionDiagnostics()
        ambiguous_streak = 0
        while True:
            diagnostics.polls += 1
            attempt = await self._single_pass(spec, require_enabled, diagnostics)
            if attempt.resolved is not None:
                diagnostics.duration_ms = int((time.monotonic() - started) * 1000)
                return attempt.resolved
            ambiguous_streak = ambiguous_streak + 1 if attempt.ambiguous else 0
            if ambiguous_streak >= self._ambiguity_confirmations or deadline.expired():
                break
            await asyncio.sleep(min(self._poll_interval_s, max(deadline.remaining(), 0.01)))

        diagnostics.duration_ms = int((time.monotonic() - started) * 1000)
        if attempt.ambiguous:
            raise ResolutionError(ResolutionFailure.AMBIGUOUS_TARGET, spec, diagnostics)
        if allow_coordinates and spec.coordinates is not None:
            diagnostics.attempts.append(
                StrategyAttempt(0, Strategy.COORDINATES, 1, 1, "coordinate fallback offered")
            )
            return ResolvedTarget(spec, Strategy.COORDINATES, None, spec.coordinates, diagnostics)
        raise ResolutionError(ResolutionFailure.TARGET_NOT_FOUND, spec, diagnostics)

    async def _single_pass(
        self, spec: TargetSpec, require_enabled: bool, diagnostics: ResolutionDiagnostics
    ) -> _Pass:
        result = _Pass()
        for index, candidate in enumerate([spec, *spec.fallbacks]):
            if candidate.frame is not None:
                raise ResolutionError(ResolutionFailure.UNSUPPORTED_TARGET, candidate, diagnostics)
            scope = await self._resolve_scope(candidate, index, diagnostics)
            semantic_tie = False
            for strategy in candidate.available_strategies():
                if strategy is Strategy.COORDINATES:
                    continue
                if (
                    strategy in SEMANTIC_STRATEGIES
                    and candidate.within is not None
                    and scope is None
                ):
                    diagnostics.attempts.append(
                        StrategyAttempt(index, strategy, 0, 0, "skipped: container not resolved")
                    )
                    continue
                if strategy in STRUCTURAL_STRATEGIES and semantic_tie:
                    diagnostics.attempts.append(
                        StrategyAttempt(
                            index, strategy, 0, 0, "skipped: cannot break a semantic tie"
                        )
                    )
                    continue
                in_scope = scope if strategy in SEMANTIC_STRATEGIES else None
                matches = await self._backend.match(candidate, strategy, in_scope)
                usable = [m for m in matches if m.visible and (m.enabled or not require_enabled)]
                diagnostics.attempts.append(
                    StrategyAttempt(
                        index,
                        strategy,
                        len(matches),
                        len(usable),
                        _detail(matches, usable, require_enabled),
                    )
                )
                if len(usable) == 1:
                    if strategy in STRUCTURAL_STRATEGIES and candidate.semantic_strategies():
                        diagnostics.drift_fallback = True
                    result.resolved = ResolvedTarget(
                        candidate, strategy, usable[0], None, diagnostics
                    )
                    return result
                if len(usable) > 1 and strategy in SEMANTIC_STRATEGIES:
                    semantic_tie = True
            result.ambiguous = result.ambiguous or semantic_tie
        return result

    async def _resolve_scope(
        self, candidate: TargetSpec, index: int, diagnostics: ResolutionDiagnostics
    ) -> MatchedElement | None:
        """Resolve the ``within`` container: exactly one visible match, first strategy wins."""
        container = candidate.within
        if container is None:
            return None
        for strategy in container.available_strategies():
            if strategy is Strategy.COORDINATES:
                continue
            matches = await self._backend.match(container, strategy)
            visible = [m for m in matches if m.visible]
            diagnostics.attempts.append(
                StrategyAttempt(
                    index,
                    strategy,
                    len(matches),
                    len(visible),
                    f"container {container.summary()}: {_detail(matches, visible, False)}",
                )
            )
            if len(visible) == 1:
                return visible[0]
        return None


def _detail(matches: list[MatchedElement], usable: list[MatchedElement], enabled: bool) -> str:
    if not matches:
        return "no match"
    if len(usable) == 1:
        return "unique" if len(matches) == 1 else "unique after filtering hidden/disabled"
    if len(usable) > 1:
        return "ambiguous"
    hidden = sum(1 for m in matches if not m.visible)
    disabled = sum(1 for m in matches if m.visible and not m.enabled)
    parts = []
    if hidden:
        parts.append(f"{hidden} hidden")
    if disabled and enabled:
        parts.append(f"{disabled} disabled")
    return "matched but unusable: " + ", ".join(parts)

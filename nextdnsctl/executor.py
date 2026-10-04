"""Applying a plan and checking that the profile converged."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from .client import APIError, Client, RateLimitError
from .config import Origin
from .model import Diff
from .planner import Op, Planner, ProfilePlan

log = logging.getLogger(__name__)

Progress = Callable[[Op, Optional[str]], None]  # called after each op with an error message or None


@dataclass
class ApplyResult:
    plan: ProfilePlan
    profile_id: Optional[str] = None
    created: bool = False
    patched: bool = False
    ops_done: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (what, why)
    aborted: Optional[str] = None
    drift: Optional[Diff] = None

    @property
    def ok(self) -> bool:
        return not self.failures and not self.aborted and not self.drift

    @property
    def partial(self) -> bool:
        """Something was written but not everything succeeded."""
        return not self.ok and (self.patched or self.ops_done > 0 or self.created)


class Executor:
    def __init__(self, client: Client, planner: Planner):
        self.client = client
        self.planner = planner

    def apply(self, plan: ProfilePlan, progress: Optional[Progress] = None, verify: bool = True) -> ApplyResult:
        result = ApplyResult(plan, plan.profile_id)
        try:
            if plan.create:
                created = self.client.create_profile(plan.name)
                result.profile_id = created["id"]
                result.created = True
                self.planner.forget_profiles()
                log.info("Created profile %s (%s)", plan.name, result.profile_id)
                plan = self.planner.plan_against(
                    plan.key, result.profile_id, plan.resolved, self.planner.live(result.profile_id)
                )
                result.plan = plan
            assert result.profile_id is not None

            if plan.patch:
                try:
                    self.client.patch_profile(result.profile_id, plan.patch)
                    result.patched = True
                except APIError as e:
                    result.failures.append(("profile update", explain_api_error(e, plan.patch_origins)))
                    return result  # all-or-nothing (A1): nothing was applied, don't continue piecemeal

            for op in plan.ops:
                error = self._run_op(result.profile_id, op)
                if error is None:
                    result.ops_done += 1
                else:
                    result.failures.append((f"{op.kind} {op.section} {op.label}", error))
                if progress:
                    progress(op, error)
        except RateLimitError as e:
            result.aborted = str(e)
            return result
        except KeyboardInterrupt:
            result.aborted = "interrupted"
            return result

        if verify and not result.failures:
            current = self.planner.live_for({"id": result.profile_id, "name": plan.name}, plan.resolved.overlay)
            after = self.planner.plan_against(plan.key, result.profile_id, plan.resolved, current)
            if after.diff:
                result.drift = after.diff
        return result

    def _run_op(self, profile_id: str, op: Op) -> Optional[str]:
        try:
            self.client.request(op.method, f"profiles/{profile_id}/{op.path}", op.body)
            return None
        except APIError as e:
            # A6: the entry is already where we want it.
            if op.kind == "add" and e.has_code("duplicate"):
                return None
            if op.kind == "remove" and e.status == 404:
                return None
            return e.describe()


def explain_api_error(error: APIError, origins: dict[str, Origin]) -> str:
    """Describe an API error, pointing at the line in the file or source that caused it (A9)."""
    message = error.describe()
    for pointer in error.pointers:
        origin = _origin_for(pointer, origins)
        if origin:
            message += f" (from {origin})"
    if error.has_code("duplicate"):
        message += " — a list contains the same entry twice"
    return message


def _origin_for(pointer: str, origins: dict[str, Origin]) -> Optional[Origin]:
    best = None
    for prefix, origin in origins.items():
        if (pointer == prefix or pointer.startswith(prefix + "/")) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, origin)
    return best[1] if best else None

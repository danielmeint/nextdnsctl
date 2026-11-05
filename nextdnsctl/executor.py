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
# Called when the profile changed after the plan was made: (old plan, new plan) -> go ahead?
OnRebase = Callable[[ProfilePlan, ProfilePlan], bool]

# A PATCH replaces whole lists (A2), so an entry someone adds between reading a list and
# writing it would be lost. Plans older than this are re-checked before such a write (e.g.
# after a confirmation prompt); younger ones were read moments ago.
FRESH_SECONDS = 5.0


@dataclass
class ApplyResult:
    plan: ProfilePlan
    profile_id: Optional[str] = None
    created: bool = False
    patches_done: int = 0
    ops_done: int = 0
    rebased: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)  # (what, why)
    aborted: Optional[str] = None
    drift: Optional[Diff] = None

    @property
    def patched(self) -> bool:
        return self.patches_done > 0

    @property
    def ok(self) -> bool:
        return not self.failures and not self.aborted and not self.drift

    @property
    def partial(self) -> bool:
        """Something was written but not everything succeeded."""
        return not self.ok and (self.patched or self.ops_done > 0 or self.created)


class Executor:
    def __init__(self, client: Client, planner: Planner, on_rebase: Optional[OnRebase] = None):
        self.client = client
        self.planner = planner
        self.on_rebase = on_rebase

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
            else:
                fresh = self._refreshed(plan)
                if fresh is not None:
                    result.rebased = True
                    result.plan = fresh
                    if self.on_rebase is not None and not self.on_rebase(plan, fresh):
                        result.aborted = "cancelled: the profile changed in NextDNS after the plan was shown"
                        return result
                    plan = fresh
            assert result.profile_id is not None

            for patch in plan.patches:
                try:
                    self.client.patch_profile(result.profile_id, patch.body)
                    result.patches_done += 1
                except APIError as e:
                    what = "profile update" + (f" ({', '.join(patch.sections)})" if len(plan.patches) > 1 else "")
                    result.failures.append((what, explain_api_error(e, patch.origins)))
                    # Each PATCH is all-or-nothing (A1): this one changed nothing; don't go on piecemeal.
                    return result

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
            # A quick edit's target is built from the live list, so entries someone else added
            # meanwhile (which per-entry writes leave alone) aren't drift.
            if plan.rebase is not None:
                after = plan.rebase(current)
            else:
                after = self.planner.plan_against(plan.key, result.profile_id, plan.resolved, current)
            if after.diff:
                result.drift = after.diff
        return result

    def _refreshed(self, plan: ProfilePlan) -> Optional[ProfilePlan]:
        """A new plan if what the plan's PATCHes would overwrite changed since it was read."""
        if plan.rebase is None or self.planner.clock() - plan.read_at < FRESH_SECONDS:
            return None
        fresh = self.planner.refresh(plan)
        if fresh is None:
            return None
        log.info("%s changed in NextDNS since it was read; planning again", plan.key)
        return plan.rebase(fresh)

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

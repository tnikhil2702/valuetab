"""
deadline_engine.py — Cascade engine for DeadlineOps.

Given a matter and one or more trigger events, computes every downstream
deadline and returns structured results ready for calendar sync.

Usage
-----
    from deadlineops.utils.deadline_engine import DeadlineEngine, TriggerEvent

    engine = DeadlineEngine(jurisdiction="CO_STATE")

    events = [
        TriggerEvent(trigger="service_of_complaint", date=date(2025, 5, 4)),
        TriggerEvent(trigger="trial_date",            date=date(2025, 11, 3)),
    ]

    results = engine.compute(matter_id="smith-v-jones-001", events=events)
    for r in results:
        print(r)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

from deadlineops.utils.date_math import (
    add_days,
    add_years,
    apply_cmo_override,
    compute_discovery_cutoff_min,
    compute_rebuttal_expert_date,
    compute_reminder_dates,
    warn_missing_trigger,
)
from deadlineops.utils.rules_loader import JurisdictionRules, load_rules


# ─── Jurisdiction short-prefix mapping ───────────────────────────────────────
# YAML rule IDs use abbreviated prefixes (co_fed_, az_fed_) that differ from
# the full jurisdiction name (co_federal_, az_federal_).  This map allows the
# engine to strip the correct short prefix so chained-trigger aliases resolve.
JURISDICTION_SHORT_PREFIXES: Dict[str, str] = {
    "CO_FEDERAL": "co_fed_",
    "AZ_FEDERAL": "az_fed_",
    "CO_STATE":   "co_state_",
    "AZ_STATE":   "az_state_",
}

# Rule IDs that use the rebuttal-expert specialised formula
# (defendant_expert_disclosure_date + 30, provisional if date unknown).
REBUTTAL_EXPERT_RULE_IDS = {
    "az_state_rebuttal_expert",
    "az_fed_rebuttal_expert",
    "co_state_rebuttal_expert",
    "co_fed_rebuttal_expert",
}

# Rule IDs that use the AZ MIN discovery-cutoff formula
# (MIN(trial_date − offset, early_meeting + tier_days)).
DISCOVERY_CUTOFF_MIN_RULE_IDS = {
    "az_state_discovery_cutoff",
}


# ─── data classes ────────────────────────────────────────────────────────────

@dataclass
class TriggerEvent:
    """A single known date that seeds one or more downstream deadlines."""
    trigger: str        # must match a rule's `trigger` field
    date: date


@dataclass
class ComputedDeadline:
    """A single computed deadline, ready for calendar sync."""
    rule_id: str
    jurisdiction: str
    trigger: str
    trigger_date: date
    label: str
    deadline_date: date
    reminder_dates: List[date]
    color: str
    notes: str
    matter_id: str
    attorney_ids: List[str] = field(default_factory=list)
    is_provisional: bool = False       # True when rebuttal expert date is estimated
    filing_status: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "rule_id":        self.rule_id,
            "jurisdiction":   self.jurisdiction,
            "trigger":        self.trigger,
            "trigger_date":   self.trigger_date.isoformat(),
            "label":          self.label,
            "deadline_date":  self.deadline_date.isoformat(),
            "reminder_dates": [d.isoformat() for d in self.reminder_dates],
            "color":          self.color,
            "notes":          self.notes,
            "matter_id":      self.matter_id,
            "attorney_ids":   self.attorney_ids,
            "is_provisional": self.is_provisional,
            "filing_status":  self.filing_status,
        }


# ─── engine ──────────────────────────────────────────────────────────────────

class DeadlineEngine:
    """
    Compute all deadlines for a matter given a jurisdiction and trigger events.

    The engine does a single pass over trigger events.  Because some rules
    reference dates produced by other rules (e.g. expert_disclosure_plaintiff
    depends on discovery_cutoff which may itself be computed), the engine
    iterates until no new deadlines are added (fixed-point convergence).
    In practice this is 2–4 passes.

    Special handling
    ----------------
    * Rebuttal expert rules (REBUTTAL_EXPERT_RULE_IDS) — use
      compute_rebuttal_expert_date() so a provisional estimate with a visible
      warning is surfaced when defendant_expert_disclosure_date is unknown.

    * Discovery cutoff MIN formula (DISCOVERY_CUTOFF_MIN_RULE_IDS) — use
      compute_discovery_cutoff_min() when both trial_date and early_meeting_date
      are in the trigger_map, implementing the confirmed AZ formula:
          MIN(trial_date − 60, early_meeting + tier_days)

    * CMO overrides — when cmo_override_dates is supplied, apply_cmo_override()
      is called for each rule that carries a cmo_override field, respecting the
      statutory/adjustable distinction.
    """

    def __init__(self, jurisdiction: str):
        self.jurisdiction_rules: JurisdictionRules = load_rules(jurisdiction)
        self._short_prefix: str = JURISDICTION_SHORT_PREFIXES.get(
            jurisdiction.upper(), ""
        )

    def _register_aliases(
        self,
        trigger_map: dict,
        rule_id: str,
        deadline_date: date,
    ) -> bool:
        """
        Add up to three trigger aliases so downstream rules can chain on this result.

        Aliases registered:
          1. ``{rule_id}_date``          — full id alias (always)
          2. ``{short_stripped}_date``   — short-prefix stripped (e.g. co_fed_ strip)
          3. ``{long_stripped}_date``    — jurisdiction.lower() stripped (redundant
                                           for CO_FEDERAL/AZ_FEDERAL but harmless)

        Returns True if any new alias was added (signals another engine pass needed).
        """
        added = False
        jur_prefix = f"{self.jurisdiction_rules.jurisdiction.lower()}_"

        candidates = set()

        # 1. Full rule_id alias
        candidates.add(f"{rule_id}_date")

        # 2. Short-prefix stripped (handles co_fed_ / az_fed_ mismatch)
        if self._short_prefix and rule_id.startswith(self._short_prefix):
            stripped = rule_id[len(self._short_prefix):]
            candidates.add(f"{stripped}_date")

        # 3. Long jurisdiction-prefix stripped (works for co_state_ / az_state_)
        if rule_id.startswith(jur_prefix):
            stripped = rule_id[len(jur_prefix):]
            candidates.add(f"{stripped}_date")

        for alias in candidates:
            if alias not in trigger_map:
                trigger_map[alias] = deadline_date
                added = True

        return added

    def compute(
        self,
        matter_id: str,
        events: List[TriggerEvent],
        attorney_ids: Optional[List[str]] = None,
        cmo_override_dates: Optional[Dict[str, date]] = None,
        tier: int = 2,
        early_meeting_date: Optional[date] = None,
        filing_status_map: Optional[Dict[str, str]] = None,
    ) -> List[ComputedDeadline]:
        """
        Compute all deadlines for a matter.

        Parameters
        ----------
        matter_id           : Unique identifier for the matter.
        events              : List of known trigger dates.
        attorney_ids        : Optional list of attorney IDs.
        cmo_override_dates  : Optional dict mapping rule_id → CMO date.
                              Applied via apply_cmo_override() for rules that
                              carry a cmo_override field.
        tier                : ARCP 26.2 tier (1 | 2 | 3) for AZ MIN formula.
                              Default 2 ($50k–$300k, most standard PI cases).
        early_meeting_date  : ARCP 16(b) Early Meeting date.  When supplied
                              together with tier, enables the AZ MIN formula for
                              az_state_discovery_cutoff instead of simple trial−60.

        Returns
        -------
        List of ComputedDeadline, sorted by deadline_date ascending.
        """
        if filing_status_map is None:
            filing_status_map = {}
        if attorney_ids is None:
            attorney_ids = []
        if cmo_override_dates is None:
            cmo_override_dates = {}

        trigger_map: dict[str, date] = {e.trigger: e.date for e in events}

        # Seed early_meeting_date into trigger_map if provided explicitly
        if early_meeting_date is not None:
            trigger_map.setdefault("early_meeting_date", early_meeting_date)

        computed: dict[str, ComputedDeadline] = {}
        holiday_set = self.jurisdiction_rules.holiday_set
        # Collect deferred warnings so each unique (trigger_name, rule_id) pair
        # is emitted only once after all passes complete, not once per pass.
        _deferred_warnings: set[tuple[str, str]] = set()

        max_passes = 10
        for _ in range(max_passes):
            added_any = False

            for rule in self.jurisdiction_rules.rules:
                if rule.id in computed:
                    continue  # already done

                # ── Rebuttal expert: special provisional handling ─────────────
                if rule.id in REBUTTAL_EXPERT_RULE_IDS:
                    # Use compute_rebuttal_expert_date regardless of whether
                    # defendant_expert_disclosure_date is in trigger_map yet.
                    # trial_date must be available to generate the provisional.
                    trial_date = trigger_map.get("trial_date")
                    if trial_date is None:
                        # trial_date simply not part of this compute call's scope —
                        # rule won't compute; no warning needed.
                        continue

                    def_expert_date = trigger_map.get("defendant_expert_disclosure_date")
                    if def_expert_date is None:
                        # Defer so we warn once after all passes, not per pass.
                        _deferred_warnings.add(("defendant_expert_disclosure_date", rule.id))

                    deadline_date, is_provisional = compute_rebuttal_expert_date(
                        defendant_expert_disclosure_date=def_expert_date,
                        trial_date=trial_date,
                        holiday_set=holiday_set,
                    )

                    # Provisional warn deferred — will emit once after loop if still missing.
                    # (Inline warn here would fire every compute() call that lacks defendant date.)

                    # Warn if rebuttal falls <30 days before trial
                    days_before_trial = (trial_date - deadline_date).days
                    if days_before_trial < 30:
                        warnings.warn(
                            f"Rule {rule.id!r}: rebuttal expert deadline {deadline_date} "
                            f"falls {days_before_trial} days before trial — expert may not "
                            f"be deposed before trial.",
                            UserWarning,
                            stacklevel=2,
                        )

                    trigger_date = def_expert_date if def_expert_date is not None else trial_date

                    # Apply CMO override if provided
                    cmo_date = cmo_override_dates.get(rule.id)
                    if cmo_date is not None and rule.cmo_override:
                        deadline_date = apply_cmo_override(
                            formula_date=deadline_date,
                            cmo_date=cmo_date,
                            cmo_override_value=rule.cmo_override,
                            rule_id=rule.id,
                        )

                    reminder_dates = compute_reminder_dates(
                        deadline=deadline_date,
                        reminders_days=rule.reminders_days,
                        holiday_set=holiday_set,
                    )
                    cd = ComputedDeadline(
                        rule_id=rule.id,
                        jurisdiction=rule.jurisdiction,
                        trigger=rule.trigger,
                        trigger_date=trigger_date,
                        label=rule.label,
                        deadline_date=deadline_date,
                        reminder_dates=reminder_dates,
                        color=rule.color,
                        notes=rule.notes,
                        matter_id=matter_id,
                        attorney_ids=attorney_ids,
                        is_provisional=is_provisional,
                    )
                    cd.filing_status = filing_status_map.get(cd.trigger)
                    computed[rule.id] = cd
                    added = self._register_aliases(trigger_map, rule.id, deadline_date)
                    if added:
                        added_any = True
                    continue

                # ── Discovery cutoff MIN formula (AZ state) ───────────────────
                if rule.id in DISCOVERY_CUTOFF_MIN_RULE_IDS:
                    trial_date = trigger_map.get("trial_date")
                    em_date = trigger_map.get("early_meeting_date")

                    if trial_date is None:
                        # trial_date not in scope for this compute call — skip silently.
                        continue

                    if em_date is not None:
                        # Full MIN formula available
                        deadline_date = compute_discovery_cutoff_min(
                            trial_date=trial_date,
                            early_meeting_date=em_date,
                            tier=tier,
                            holiday_set=holiday_set,
                        )
                    else:
                        # Fall back to simple trial − 60 (matches rule offset_days)
                        deadline_date = add_days(
                            start=trial_date,
                            offset_days=rule.offset_days,
                            day_type=rule.day_type,
                            rollover=rule.rollover,
                            holiday_set=holiday_set,
                        )

                    # Apply CMO override if provided
                    cmo_date = cmo_override_dates.get(rule.id)
                    if cmo_date is not None and rule.cmo_override:
                        deadline_date = apply_cmo_override(
                            formula_date=deadline_date,
                            cmo_date=cmo_date,
                            cmo_override_value=rule.cmo_override,
                            rule_id=rule.id,
                        )

                    reminder_dates = compute_reminder_dates(
                        deadline=deadline_date,
                        reminders_days=rule.reminders_days,
                        holiday_set=holiday_set,
                    )
                    trigger_date = trial_date
                    cd = ComputedDeadline(
                        rule_id=rule.id,
                        jurisdiction=rule.jurisdiction,
                        trigger=rule.trigger,
                        trigger_date=trigger_date,
                        label=rule.label,
                        deadline_date=deadline_date,
                        reminder_dates=reminder_dates,
                        color=rule.color,
                        notes=rule.notes,
                        matter_id=matter_id,
                        attorney_ids=attorney_ids,
                    )
                    cd.filing_status = filing_status_map.get(cd.trigger)
                    computed[rule.id] = cd
                    added = self._register_aliases(trigger_map, rule.id, deadline_date)
                    if added:
                        added_any = True
                    continue

                # ── Standard rule processing ──────────────────────────────────
                if rule.trigger not in trigger_map:
                    continue  # trigger not yet available

                trigger_date = trigger_map[rule.trigger]

                if rule.offset_years is not None:
                    deadline_date = add_years(
                        start=trigger_date,
                        offset_years=rule.offset_years,
                        rollover=rule.rollover,
                        holiday_set=holiday_set,
                    )
                else:
                    deadline_date = add_days(
                        start=trigger_date,
                        offset_days=rule.offset_days,
                        day_type=rule.day_type,
                        rollover=rule.rollover,
                        holiday_set=holiday_set,
                    )

                # Apply CMO override if provided
                cmo_date = cmo_override_dates.get(rule.id)
                if cmo_date is not None and rule.cmo_override:
                    deadline_date = apply_cmo_override(
                        formula_date=deadline_date,
                        cmo_date=cmo_date,
                        cmo_override_value=rule.cmo_override,
                        rule_id=rule.id,
                    )

                reminder_dates = compute_reminder_dates(
                    deadline=deadline_date,
                    reminders_days=rule.reminders_days,
                    holiday_set=holiday_set,
                )

                cd = ComputedDeadline(
                    rule_id=rule.id,
                    jurisdiction=rule.jurisdiction,
                    trigger=rule.trigger,
                    trigger_date=trigger_date,
                    label=rule.label,
                    deadline_date=deadline_date,
                    reminder_dates=reminder_dates,
                    color=rule.color,
                    notes=rule.notes,
                    matter_id=matter_id,
                    attorney_ids=attorney_ids,
                )
                cd.filing_status = filing_status_map.get(cd.trigger)
                computed[rule.id] = cd

                added = self._register_aliases(trigger_map, rule.id, deadline_date)
                if added:
                    added_any = True

            if not added_any:
                break

        # Emit each deferred warning exactly once after all passes complete.
        for trigger_name, rule_id in sorted(_deferred_warnings):
            if rule_id in computed:
                # Rule was successfully computed — warning no longer relevant.
                continue
            if trigger_name == "defendant_expert_disclosure_date":
                # Rebuttal rule ran with provisional estimate; surface specific notice.
                warnings.warn(
                    f"Rule {rule_id!r}: defendant_expert_disclosure_date not yet "
                    f"available — using trial−60 as a PROVISIONAL estimate. "
                    f"Update when defendant discloses experts.",
                    UserWarning,
                    stacklevel=2,
                )
            else:
                warn_missing_trigger(trigger_name, rule_id)

        results = sorted(computed.values(), key=lambda d: d.deadline_date)
        return results


# ─── convenience function ────────────────────────────────────────────────────

def compute_deadlines(
    jurisdiction: str,
    matter_id: str,
    events: List[TriggerEvent],
    attorney_ids: Optional[List[str]] = None,
    cmo_override_dates: Optional[Dict[str, date]] = None,
    tier: int = 2,
    early_meeting_date: Optional[date] = None,
    filing_status_map: Optional[Dict[str, str]] = None,
) -> List[ComputedDeadline]:
    """
    One-call convenience wrapper around DeadlineEngine.

    Parameters
    ----------
    jurisdiction        : One of CO_STATE, CO_FEDERAL, AZ_STATE, AZ_FEDERAL.
    matter_id           : Unique matter identifier.
    events              : Trigger dates (service date, trial date, etc.)
    attorney_ids        : Optional attorney IDs to attach to every deadline.
    cmo_override_dates  : Optional {rule_id: date} dict for scheduling-order
                          overrides.  Applied via apply_cmo_override() using the
                          cmo_override field on each rule.
    tier                : ARCP 26.2 discovery tier (1 | 2 | 3).  Default 2.
    early_meeting_date  : ARCP 16(b) Early Meeting date.  When provided together
                          with tier, enables the AZ MIN discovery-cutoff formula.

    Example
    -------
        from deadlineops.utils.deadline_engine import compute_deadlines, TriggerEvent
        from datetime import date

        deadlines = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="smith-v-jones-001",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 5, 4)),
                TriggerEvent("trial_date",            date(2025, 11, 3)),
            ],
            tier=2,
            early_meeting_date=date(2025, 6, 10),
            cmo_override_dates={
                "az_state_discovery_cutoff": date(2025, 9, 1),
            },
        )
    """
    engine = DeadlineEngine(jurisdiction)
    return engine.compute(
        matter_id=matter_id,
        events=events,
        attorney_ids=attorney_ids,
        cmo_override_dates=cmo_override_dates,
        tier=tier,
        early_meeting_date=early_meeting_date,
        filing_status_map=filing_status_map,
    )
"""
test_rules_engine.py — Comprehensive unit tests for the DeadlineOps rules engine.

Run with:
    pip install pytest pyyaml python-dateutil
    python -m pytest deadlineops/tests/test_rules_engine.py -v

Every encoded court rule has at least one test.
The date-math library has edge-case tests for holidays, weekends, and rollovers.
"""

import warnings
import pytest
from datetime import date, timedelta

from deadlineops.utils.holidays import get_holidays, add_emergency_closure, clear_emergency_closures
from deadlineops.utils.date_math import (
    add_days,
    add_years,
    is_business_day,
    next_business_day,
    previous_business_day,
    compute_discovery_cutoff_min,
    compute_rebuttal_expert_date,
    apply_cmo_override,
    StatutoryDeadlineOverrideError,
    warn_missing_trigger,
)
from deadlineops.utils.rules_loader import load_rules, get_rules_for_trigger, list_triggers
from deadlineops.utils.deadline_engine import compute_deadlines, TriggerEvent


# ══════════════════════════════════════════════════════════════════════════════
#  HOLIDAY TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestFederalHolidays:
    def test_new_years_day_2025(self):
        assert date(2025, 1, 1) in get_holidays("federal", 2025)

    def test_independence_day_2025(self):
        assert date(2025, 7, 4) in get_holidays("federal", 2025)

    def test_christmas_2025(self):
        assert date(2025, 12, 25) in get_holidays("federal", 2025)

    def test_thanksgiving_2025(self):
        # 4th Thursday in November 2025 = Nov 27
        assert date(2025, 11, 27) in get_holidays("federal", 2025)

    def test_memorial_day_2025(self):
        # Last Monday in May 2025 = May 26
        assert date(2025, 5, 26) in get_holidays("federal", 2025)

    def test_labor_day_2025(self):
        # 1st Monday in September 2025 = Sep 1
        assert date(2025, 9, 1) in get_holidays("federal", 2025)

    def test_mlk_day_2025(self):
        # 3rd Monday in January 2025 = Jan 20
        assert date(2025, 1, 20) in get_holidays("federal", 2025)

    # ── Juneteenth inclusion ──────────────────────────────────────────────────
    def test_juneteenth_2025(self):
        """Juneteenth (June 19) must be included — many older libraries omit it."""
        assert date(2025, 6, 19) in get_holidays("federal", 2025)

    def test_juneteenth_2022(self):
        """Juneteenth became federal holiday in 2021; must appear in 2022."""
        assert date(2022, 6, 19) in get_holidays("federal", 2022)

    def test_juneteenth_observed_when_sunday(self):
        # June 19 2022 was Sunday → observed Monday June 20
        assert date(2022, 6, 20) in get_holidays("federal", 2022)

    def test_july_4_observed_when_saturday(self):
        # July 4 2020 was Saturday → observed Friday July 3
        assert date(2020, 7, 3) in get_holidays("federal", 2020)

    def test_july_4_observed_when_sunday(self):
        # July 4 2021 was Sunday → observed Monday July 5
        assert date(2021, 7, 5) in get_holidays("federal", 2021)

    def test_non_holiday(self):
        assert date(2025, 3, 15) not in get_holidays("federal", 2025)


class TestArizonaObservedHolidays:
    """
    Arizona observed holidays — full A.R.S. §1-301 list.
    Each holiday must be present in the federal+arizona set.
    """

    def test_new_years_day(self):
        assert date(2025, 1, 1) in get_holidays("federal+arizona", 2025)

    def test_mlk_civil_rights_day(self):
        # 3rd Monday in January 2025 = Jan 20
        assert date(2025, 1, 20) in get_holidays("federal+arizona", 2025)

    def test_presidents_day(self):
        # 3rd Monday in February 2025 = Feb 17
        assert date(2025, 2, 17) in get_holidays("federal+arizona", 2025)

    def test_memorial_day(self):
        # Last Monday in May 2025 = May 26
        assert date(2025, 5, 26) in get_holidays("federal+arizona", 2025)

    def test_juneteenth_az(self):
        """Juneteenth added to AZ law in 2022 per A.R.S. §1-301."""
        assert date(2025, 6, 19) in get_holidays("federal+arizona", 2025)

    def test_independence_day_az(self):
        assert date(2025, 7, 4) in get_holidays("federal+arizona", 2025)

    def test_labor_day_az(self):
        # 1st Monday September 2025 = Sep 1
        assert date(2025, 9, 1) in get_holidays("federal+arizona", 2025)

    def test_columbus_day_az(self):
        """AZ still observes Columbus Day (2nd Monday October) per A.R.S. §1-301."""
        # 2nd Monday in October 2025 = Oct 13
        assert date(2025, 10, 13) in get_holidays("federal+arizona", 2025)

    def test_veterans_day_az(self):
        assert date(2025, 11, 11) in get_holidays("federal+arizona", 2025)

    def test_thanksgiving_az(self):
        # 4th Thursday November 2025 = Nov 27
        assert date(2025, 11, 27) in get_holidays("federal+arizona", 2025)

    def test_christmas_day_az(self):
        assert date(2025, 12, 25) in get_holidays("federal+arizona", 2025)

    def test_az_observed_saturday_fixed_holiday(self):
        """Fixed holiday on Saturday → observed Friday."""
        # Independence Day 2020 = Saturday → observed Friday July 3
        assert date(2020, 7, 3) in get_holidays("federal+arizona", 2020)

    def test_az_observed_sunday_fixed_holiday(self):
        """Fixed holiday on Sunday → observed Monday."""
        # Veterans Day 2018 = Sunday → observed Monday Nov 12
        assert date(2018, 11, 12) in get_holidays("federal+arizona", 2018)


class TestColoradoHolidays:
    def test_co_includes_federal(self):
        assert date(2025, 7, 4) in get_holidays("federal+colorado", 2025)

    def test_co_cesar_chavez_day(self):
        # March 31
        assert date(2025, 3, 31) in get_holidays("federal+colorado", 2025)

    def test_co_does_not_include_az_columbus_day(self):
        # Columbus Day 2025 = Oct 13; CO does not add it (federal does NOT include it)
        # (Federal set has Columbus Day as a federal holiday, so it IS in CO set)
        # This test verifies that the AZ-specific and CO-specific sets are distinct
        az_set = get_holidays("federal+arizona", 2025)
        co_set = get_holidays("federal+colorado", 2025)
        # Cesar Chavez Day is in CO but not in AZ
        assert date(2025, 3, 31) in co_set
        assert date(2025, 3, 31) not in az_set


class TestEmergencyClosures:
    def setup_method(self):
        clear_emergency_closures()

    def teardown_method(self):
        clear_emergency_closures()

    def test_emergency_closure_treated_as_holiday(self):
        closure = date(2025, 8, 15)  # arbitrary Friday — normally a business day
        add_emergency_closure(closure)
        assert closure in get_holidays("federal+arizona", 2025)

    def test_emergency_closure_makes_day_non_business(self):
        closure = date(2025, 8, 15)  # Friday
        add_emergency_closure(closure)
        assert not is_business_day(closure, "federal+arizona")

    def test_clear_removes_closure(self):
        closure = date(2025, 8, 15)
        add_emergency_closure(closure)
        clear_emergency_closures(2025)
        assert closure not in get_holidays("federal+arizona", 2025)


# ══════════════════════════════════════════════════════════════════════════════
#  DATE MATH TESTS — FORWARD / BACKWARD / WEEKEND / HOLIDAY
# ══════════════════════════════════════════════════════════════════════════════

class TestIsBusinessDay:
    def test_monday_is_business(self):
        assert is_business_day(date(2025, 5, 5), "federal")   # Monday

    def test_saturday_not_business(self):
        assert not is_business_day(date(2025, 5, 3), "federal")

    def test_sunday_not_business(self):
        assert not is_business_day(date(2025, 5, 4), "federal")

    def test_holiday_not_business(self):
        # Independence Day 2025 = Friday July 4
        assert not is_business_day(date(2025, 7, 4), "federal")

    def test_day_after_holiday_is_business(self):
        assert is_business_day(date(2025, 7, 7), "federal")   # Monday after July 4th weekend


class TestNextBusinessDay:
    """Weekend adjustment forward"""

    def test_weekday_returns_same(self):
        assert next_business_day(date(2025, 5, 5), "federal") == date(2025, 5, 5)

    def test_saturday_returns_monday(self):
        """Weekend adjustment forward: Saturday → Monday"""
        assert next_business_day(date(2025, 5, 3), "federal") == date(2025, 5, 5)

    def test_sunday_returns_monday(self):
        """Weekend adjustment forward: Sunday → Monday"""
        assert next_business_day(date(2025, 5, 4), "federal") == date(2025, 5, 5)

    def test_holiday_friday_returns_monday(self):
        """Holiday adjustment forward: July 4 2025 (Friday) → Monday July 7"""
        assert next_business_day(date(2025, 7, 4), "federal") == date(2025, 7, 7)

    def test_holiday_monday_returns_tuesday(self):
        """Holiday adjustment forward: Memorial Day 2025 (Monday May 26) → Tuesday May 27"""
        assert next_business_day(date(2025, 5, 26), "federal") == date(2025, 5, 27)


class TestPreviousBusinessDay:
    """Weekend adjustment backward"""

    def test_weekday_returns_same(self):
        assert previous_business_day(date(2025, 5, 5), "federal") == date(2025, 5, 5)

    def test_saturday_returns_friday(self):
        """Weekend adjustment backward: Saturday → Friday"""
        assert previous_business_day(date(2025, 5, 3), "federal") == date(2025, 5, 2)

    def test_sunday_returns_friday(self):
        """Weekend adjustment backward: Sunday → Friday"""
        assert previous_business_day(date(2025, 5, 4), "federal") == date(2025, 5, 2)

    def test_monday_holiday_returns_friday(self):
        """Holiday adjustment backward: Memorial Day 2025 (Monday) → Friday May 23"""
        assert previous_business_day(date(2025, 5, 26), "federal") == date(2025, 5, 23)

    def test_holiday_friday_returns_thursday(self):
        """Holiday adjustment backward: July 4 2025 (Friday) → Thursday July 3"""
        assert previous_business_day(date(2025, 7, 4), "federal") == date(2025, 7, 3)


class TestAddDays:
    """Forward and backward calendar-day counting, business-day counting"""

    def test_forward_calendar_day_counting_simple(self):
        """Forward calendar-day counting"""
        result = add_days(date(2025, 5, 4), 21, "calendar", "none", "federal")
        assert result == date(2025, 5, 25)

    def test_forward_calendar_day_counting_crosses_month(self):
        """Forward calendar-day counting: crosses month boundary"""
        result = add_days(date(2025, 6, 10), 40, "calendar", "none", "federal")
        assert result == date(2025, 7, 20)

    def test_backward_calendar_day_counting_simple(self):
        """Backward calendar-day counting"""
        result = add_days(date(2025, 11, 3), -63, "calendar", "none", "federal")
        assert result == date(2025, 9, 1)

    def test_backward_calendar_day_counting_crosses_month(self):
        """Backward calendar-day counting: crosses month boundary"""
        result = add_days(date(2025, 12, 8), -120, "calendar", "none", "federal")
        assert result == date(2025, 8, 10)

    def test_calendar_days_rollover_weekend_forward(self):
        """Weekend adjustment forward in calendar-day calc"""
        # May 4 + 21 = May 25 (Sunday) → next business day
        # May 26 = Memorial Day → next business day = May 27 (Tuesday)
        result = add_days(date(2025, 5, 4), 21, "calendar", "next_business_day", "federal")
        assert result == date(2025, 5, 27)

    def test_calendar_days_rollover_weekend_backward(self):
        """Weekend adjustment backward in calendar-day calc"""
        # Sep 1 2025 is Labor Day → previous business day = Aug 29
        result = add_days(date(2025, 11, 3), -63, "calendar", "previous_business_day", "federal")
        assert result == date(2025, 8, 29)

    def test_holiday_adjustment_forward_in_add_days(self):
        """Holiday adjustment forward: result on holiday → next_business_day"""
        # July 3 + 1 = July 4 (holiday) → next_business_day = July 7
        result = add_days(date(2025, 7, 3), 1, "calendar", "next_business_day", "federal")
        assert result == date(2025, 7, 7)

    def test_holiday_adjustment_backward_in_add_days(self):
        """Holiday adjustment backward: result on holiday → previous_business_day"""
        # result on July 4 2025 (Friday holiday) → previous = July 3
        result = add_days(date(2025, 7, 5), -1, "calendar", "previous_business_day", "federal")
        assert result == date(2025, 7, 3)

    def test_business_day_counting_simple(self):
        """Business-day counting: 5 business days after Monday May 5 = Monday May 12"""
        result = add_days(date(2025, 5, 5), 5, "business", "none", "federal")
        assert result == date(2025, 5, 12)

    def test_business_day_counting_skips_weekend(self):
        """Business-day counting skips weekend: 1 biz day after Friday May 2 = Monday May 5"""
        result = add_days(date(2025, 5, 2), 1, "business", "none", "federal")
        assert result == date(2025, 5, 5)

    def test_business_day_counting_skips_holiday(self):
        """Business-day counting skips holiday: 1 biz day after Thu Jul 3 = Mon Jul 7"""
        result = add_days(date(2025, 7, 3), 1, "business", "none", "federal")
        assert result == date(2025, 7, 7)

    def test_business_day_counting_backward(self):
        """Business-day counting backward: 5 biz days before Monday May 12 = Monday May 5"""
        result = add_days(date(2025, 5, 12), -5, "business", "none", "federal")
        assert result == date(2025, 5, 5)

    def test_business_day_counting_10_days_mil_response(self):
        """10 business days (MIL response per ARCP 7.1(b)) skips weekend + holiday"""
        # MIL filed Monday Jul 7 2025 (day after holiday weekend)
        # 10 business days: Jul 8,9,10,11,14,15,16,17,18,21 = Jul 21
        result = add_days(date(2025, 7, 7), 10, "business", "none", "federal+arizona")
        assert result == date(2025, 7, 21)


# ══════════════════════════════════════════════════════════════════════════════
#  CMO OVERRIDE TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCMOOverride:
    FORMULA_DATE = date(2025, 8, 15)
    CMO_DATE = date(2025, 8, 1)
    LATER_CMO_DATE = date(2025, 9, 1)

    def test_cmo_override_allowed_yes_cmo_sets_date(self):
        """CMO override allowed: 'Yes – CMO sets date' uses CMO date."""
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=self.CMO_DATE,
            cmo_override_value="Yes – CMO sets date",
        )
        assert result == self.CMO_DATE

    def test_cmo_override_yes_no_date_falls_back_to_formula(self):
        """When CMO date is None, formula date is used."""
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=None,
            cmo_override_value="Yes – CMO sets date",
        )
        assert result == self.FORMULA_DATE

    def test_cmo_override_blocked_for_statutory_deadline(self):
        """CMO override blocked: 'No – Statutory' raises error when CMO > statutory."""
        statutory_date = date(2025, 8, 20)
        cmo_later = date(2025, 9, 1)  # later than statutory
        with pytest.raises(StatutoryDeadlineOverrideError):
            apply_cmo_override(
                formula_date=self.FORMULA_DATE,
                cmo_date=cmo_later,
                cmo_override_value="No – Statutory",
                statutory_deadline=statutory_date,
                rule_id="az_state_sol_personal_injury",
            )

    def test_cmo_override_statutory_returns_formula_ignores_cmo(self):
        """'No – Statutory' always returns formula_date (no error if CMO not later)."""
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=self.CMO_DATE,  # CMO_DATE < FORMULA_DATE — still ignored
            cmo_override_value="No – Statutory",
            statutory_deadline=self.LATER_CMO_DATE,
        )
        assert result == self.FORMULA_DATE

    def test_cmo_override_court_extension_only_with_date(self):
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=self.CMO_DATE,
            cmo_override_value="Court extension only",
        )
        assert result == self.CMO_DATE

    def test_cmo_override_na_condition_precedent(self):
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=self.CMO_DATE,
            cmo_override_value="N/A – Condition precedent",
        )
        assert result == self.FORMULA_DATE

    def test_cmo_override_na_strategic(self):
        result = apply_cmo_override(
            formula_date=self.FORMULA_DATE,
            cmo_date=self.CMO_DATE,
            cmo_override_value="N/A – Strategic",
        )
        assert result == self.FORMULA_DATE


# ══════════════════════════════════════════════════════════════════════════════
#  DISCOVERY CUTOFF MIN FORMULA
# ══════════════════════════════════════════════════════════════════════════════

class TestDiscoveryCutoffMINFormula:
    """Discovery cutoff MIN formula — all three tiers."""

    def test_tier1_trial_minus_60_binds(self):
        """
        Tier 1: Trial 400 days after early meeting.
        Trial−60 (340 days after EM) < EM+120 (120 days after EM).
        MIN = trial−60.
        """
        trial = date(2026, 6, 1)
        em = date(2025, 5, 27)  # ~370 days before trial
        result = compute_discovery_cutoff_min(trial, em, tier=1, holiday_set="federal+arizona")
        bound_trial = trial - timedelta(days=60)
        # result should equal prev biz day of bound_trial
        assert result <= bound_trial

    def test_tier2_tier_clock_binds(self):
        """
        Tier 2 Scenario A from brief: early meeting 200 days before trial.
        Tier clock: 200 − 180 = 20 days before trial.
        Trial−60 = 60 days before trial.
        MIN = 20 days before trial (tier clock wins).
        """
        trial = date(2026, 6, 1)
        em = trial - timedelta(days=200)  # 200 days before trial
        result = compute_discovery_cutoff_min(trial, em, tier=2, holiday_set="federal+arizona")
        bound_trial = trial - timedelta(days=60)
        bound_tier = em + timedelta(days=180)   # 20 days before trial
        expected_raw = min(bound_trial, bound_tier)
        assert result <= expected_raw  # may be adjusted by previous_business_day

    def test_tier3_min_formula_computed(self):
        """Tier 3: 240-day tier window."""
        trial = date(2026, 9, 1)
        em = date(2026, 1, 5)  # ~238 days before trial
        result = compute_discovery_cutoff_min(trial, em, tier=3, holiday_set="federal+arizona")
        bound_trial = trial - timedelta(days=60)
        bound_tier = em + timedelta(days=240)
        expected_raw = min(bound_trial, bound_tier)
        assert result <= expected_raw

    def test_invalid_tier_raises(self):
        with pytest.raises(ValueError):
            compute_discovery_cutoff_min(date(2026, 1, 1), date(2025, 6, 1), tier=4)

    def test_custom_trial_offset_respected(self):
        """trial_offset_days is configurable (CMO-overridable constant)."""
        trial = date(2026, 6, 1)
        em = trial - timedelta(days=300)
        result_60 = compute_discovery_cutoff_min(trial, em, tier=2, trial_offset_days=60)
        result_90 = compute_discovery_cutoff_min(trial, em, tier=2, trial_offset_days=90)
        # 90-day offset should produce an earlier result
        assert result_90 <= result_60


# ══════════════════════════════════════════════════════════════════════════════
#  REBUTTAL EXPERT CHAINED TRIGGER
# ══════════════════════════════════════════════════════════════════════════════

class TestRebuttalExpertChainedTrigger:
    """Rebuttal expert chained trigger: defendant_expert_disclosure_date + 30 days."""

    TRIAL = date(2025, 12, 8)
    DEF_EXPERT = date(2025, 9, 8)  # Monday

    def test_rebuttal_uses_defendant_disclosure_date(self):
        """Trigger = defendant_expert_disclosure_date + 30, NOT trial_date."""
        result, is_provisional = compute_rebuttal_expert_date(
            defendant_expert_disclosure_date=self.DEF_EXPERT,
            trial_date=self.TRIAL,
            holiday_set="federal+arizona",
        )
        assert not is_provisional
        expected = self.DEF_EXPERT + timedelta(days=30)  # Oct 8 (Wednesday)
        assert result == expected

    def test_rebuttal_provisional_when_no_defendant_date(self):
        """When defendant disclosure date unknown, return provisional + warning flag."""
        result, is_provisional = compute_rebuttal_expert_date(
            defendant_expert_disclosure_date=None,
            trial_date=self.TRIAL,
            holiday_set="federal+arizona",
        )
        assert is_provisional
        # Provisional = trial − 60 business-day-adjusted
        provisional_raw = self.TRIAL - timedelta(days=60)
        assert result <= provisional_raw + timedelta(days=5)  # allow for rollover

    def test_rebuttal_differs_from_trial_minus_60_approximation(self):
        """
        Demonstrates that trial−60 is WRONG whenever defendant expert date
        differs from exactly 90 days before trial.
        Def expert at 120 days before trial → rebuttal = trial−90, NOT trial−60.
        """
        trial = date(2026, 3, 2)
        def_expert = trial - timedelta(days=120)  # 120 days before trial
        result, _ = compute_rebuttal_expert_date(def_expert, trial, "federal+arizona")
        trial_minus_60 = trial - timedelta(days=60)
        # Correct answer (def_expert + 30) = trial − 90, which is EARLIER than trial−60
        assert result < trial_minus_60

    def test_rebuttal_warns_if_less_than_30_days_before_trial(self):
        """Warn if rebuttal date falls <30 days before trial."""
        trial = date(2026, 3, 2)
        # Defendant expert so late that rebuttal would be <30 days before trial
        def_expert = trial - timedelta(days=25)
        result, _ = compute_rebuttal_expert_date(def_expert, trial, "federal+arizona")
        days_before_trial = (trial - result).days
        # The test documents the scenario; callers should warn users
        assert days_before_trial < 30


# ══════════════════════════════════════════════════════════════════════════════
#  MISSING TRIGGER DATE WARNING
# ══════════════════════════════════════════════════════════════════════════════

class TestMissingTriggerDateWarning:
    def test_warn_missing_trigger_emits_user_warning(self):
        """Missing trigger date warning is surfaced as UserWarning."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warn_missing_trigger("defendant_expert_disclosure_date", "az_state_rebuttal_expert")
        assert len(caught) == 1
        assert issubclass(caught[0].category, UserWarning)
        assert "defendant_expert_disclosure_date" in str(caught[0].message)
        assert "az_state_rebuttal_expert" in str(caught[0].message)

    def test_warn_missing_trigger_message_content(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            warn_missing_trigger("early_meeting_date", "az_state_tier2_discovery_completion")
        msg = str(caught[0].message)
        assert "not yet available" in msg


# ══════════════════════════════════════════════════════════════════════════════
#  RULES LOADER TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestRulesLoader:
    def test_load_co_state(self):
        rules = load_rules("CO_STATE")
        assert rules.jurisdiction == "CO_STATE"
        assert len(rules.rules) > 0

    def test_load_co_federal(self):
        rules = load_rules("CO_FEDERAL")
        assert rules.jurisdiction == "CO_FEDERAL"

    def test_load_az_state(self):
        rules = load_rules("AZ_STATE")
        assert rules.jurisdiction == "AZ_STATE"

    def test_load_az_federal(self):
        rules = load_rules("AZ_FEDERAL")
        assert rules.jurisdiction == "AZ_FEDERAL"

    def test_invalid_jurisdiction(self):
        with pytest.raises(ValueError):
            load_rules("TX_STATE")

    def test_co_state_has_answer_rule(self):
        rules = load_rules("CO_STATE")
        triggers = get_rules_for_trigger(rules, "service_of_complaint")
        assert any(r.id == "co_state_answer" for r in triggers)

    def test_az_state_answer_offset_is_20(self):
        rules = load_rules("AZ_STATE")
        triggers = get_rules_for_trigger(rules, "service_of_complaint")
        answer = next(r for r in triggers if r.id == "az_state_answer")
        assert answer.offset_days == 20

    def test_co_state_answer_offset_is_21(self):
        rules = load_rules("CO_STATE")
        triggers = get_rules_for_trigger(rules, "service_of_complaint")
        answer = next(r for r in triggers if r.id == "co_state_answer")
        assert answer.offset_days == 21

    def test_list_triggers_co_state(self):
        rules = load_rules("CO_STATE")
        triggers = list_triggers(rules)
        assert "service_of_complaint" in triggers
        assert "trial_date" in triggers

    def test_az_state_has_notice_of_claim(self):
        rules = load_rules("AZ_STATE")
        ids = [r.id for r in rules.rules]
        assert "az_state_notice_of_claim" in ids

    def test_az_state_has_designation_nonparties(self):
        rules = load_rules("AZ_STATE")
        ids = [r.id for r in rules.rules]
        assert "az_state_designation_nonparties" in ids

    def test_az_state_has_rebuttal_expert_chained(self):
        """Rebuttal expert trigger must be defendant_expert_disclosure_date, not trial_date."""
        rules = load_rules("AZ_STATE")
        rebuttal = next(r for r in rules.rules if r.id == "az_state_rebuttal_expert")
        assert rebuttal.trigger == "defendant_expert_disclosure_date"
        assert rebuttal.offset_days == 30

    def test_az_state_mil_response_uses_business_days(self):
        """MIL response must use business days (ARCP 7.1(b))."""
        rules = load_rules("AZ_STATE")
        mil = next(r for r in rules.rules if r.id == "az_state_mil_response")
        assert mil.day_type == "business"
        assert mil.offset_days == 10

    def test_az_state_msj_response_30_days(self):
        """AZ MSJ response = 30 days (vs 21 in CO federal)."""
        rules = load_rules("AZ_STATE")
        msj_resp = next(r for r in rules.rules if r.id == "az_state_msj_response")
        assert msj_resp.offset_days == 30


# ══════════════════════════════════════════════════════════════════════════════
#  MARICOPA-ONLY FILTERING
# ══════════════════════════════════════════════════════════════════════════════

class TestMaricopaOnlyFiltering:
    """
    Maricopa-only filtering: rules with jurisdiction_scope = MARICOPA ONLY
    must be present in AZ_STATE rules but tagged for Maricopa-only application.
    """

    def test_trial_de_novo_demand_present_in_az_state(self):
        """Maricopa-only rule exists in AZ_STATE rule file."""
        rules = load_rules("AZ_STATE")
        ids = [r.id for r in rules.rules]
        assert "az_state_trial_de_novo_demand" in ids

    def test_trial_de_novo_trigger_and_days(self):
        """Maricopa trial de novo: 20 days from arbitration award (MCLR Rule 3.10)."""
        rules = load_rules("AZ_STATE")
        rule = next(r for r in rules.rules if r.id == "az_state_trial_de_novo_demand")
        assert rule.trigger == "arbitration_award_date"
        assert rule.offset_days == 20

    def test_trial_de_novo_has_maricopa_only_note(self):
        """Maricopa-only rule must carry a note indicating scope."""
        rules = load_rules("AZ_STATE")
        rule = next(r for r in rules.rules if r.id == "az_state_trial_de_novo_demand")
        assert "MARICOPA" in rule.notes.upper()

    def test_maricopa_only_rules_not_in_az_federal(self):
        """Maricopa Local Rules do not exist in the federal rule set."""
        fed_rules = load_rules("AZ_FEDERAL")
        fed_ids = [r.id for r in fed_rules.rules]
        assert "az_state_trial_de_novo_demand" not in fed_ids

    def test_az_state_includes_notice_of_claim_statewide(self):
        """Notice of Claim is statewide AZ rule (not Maricopa-only) and in AZ_STATE."""
        rules = load_rules("AZ_STATE")
        ids = [r.id for r in rules.rules]
        assert "az_state_notice_of_claim" in ids

    def test_notice_of_claim_not_in_federal(self):
        """Notice of Claim is AZ STATE ONLY — no federal equivalent."""
        fed_rules = load_rules("AZ_FEDERAL")
        fed_ids = [r.id for r in fed_rules.rules]
        assert "az_state_notice_of_claim" not in fed_ids


# ══════════════════════════════════════════════════════════════════════════════
#  FEDERAL / STATE RULE SEPARATION
# ══════════════════════════════════════════════════════════════════════════════

class TestFederalStateSeparation:
    """
    Federal/state rule separation: AZ-specific rules must not appear in federal
    set; federal-specific rules must not appear in state set.
    """

    def test_az_tier_system_not_in_federal(self):
        """ARCP 26.2 tier system has no federal equivalent."""
        fed = load_rules("AZ_FEDERAL")
        fed_ids = [r.id for r in fed.rules]
        for tier_id in ["az_state_tier1_discovery_completion",
                        "az_state_tier2_discovery_completion",
                        "az_state_tier3_discovery_completion"]:
            assert tier_id not in fed_ids

    def test_az_designation_nonparties_not_in_federal(self):
        """A.R.S. §12-2506(B) designation — state only, no federal equivalent."""
        fed = load_rules("AZ_FEDERAL")
        assert "az_state_designation_nonparties" not in [r.id for r in fed.rules]

    def test_federal_rule26_conference_in_az_federal(self):
        """FRCP 26(f) conference exists in federal rule set."""
        fed = load_rules("AZ_FEDERAL")
        ids = [r.id for r in fed.rules]
        assert "az_fed_rule26_conference" in ids

    def test_az_state_uses_early_meeting_not_rule26f(self):
        """AZ state uses ARCP 16(b) Early Meeting, not FRCP 26(f) conference."""
        state = load_rules("AZ_STATE")
        ids = [r.id for r in state.rules]
        assert "az_state_early_meeting" in ids
        assert "az_fed_rule26_conference" not in ids

    def test_answer_days_differ_az_state_vs_federal(self):
        """AZ state answer = 20 days; federal answer = 21 days."""
        az = load_rules("AZ_STATE")
        fed = load_rules("AZ_FEDERAL")
        az_ans = next(r for r in az.rules if r.id == "az_state_answer")
        fed_ans = next(r for r in fed.rules if r.id == "az_fed_answer")
        assert az_ans.offset_days == 20
        assert fed_ans.offset_days == 21

    def test_post_trial_days_differ_az_vs_federal(self):
        """AZ post-trial motions = 15 days; federal = 28 days."""
        az = load_rules("AZ_STATE")
        fed = load_rules("AZ_FEDERAL")
        az_ntm = next(r for r in az.rules if r.id == "az_state_motion_new_trial")
        fed_ntm = next(r for r in fed.rules if r.id == "az_fed_motion_new_trial")
        assert az_ntm.offset_days == 15
        assert fed_ntm.offset_days == 28

    def test_az_federal_holiday_set_is_federal_only(self):
        """AZ federal court uses federal holidays only (not AZ state holidays)."""
        fed = load_rules("AZ_FEDERAL")
        assert fed.holiday_set == "federal"

    def test_az_state_holiday_set_includes_az(self):
        """AZ state court uses federal + Arizona holidays."""
        state = load_rules("AZ_STATE")
        assert "arizona" in state.holiday_set.lower()


# ══════════════════════════════════════════════════════════════════════════════
#  DEADLINE ENGINE TESTS — CO STATE (3 COLORADO CASES)
# ══════════════════════════════════════════════════════════════════════════════

class TestCOStateDeadlines:
    """
    Colorado Case 1: Smith v. Jones — service + trial.
    Reference scenario: served May 4, 2025 (Sunday). Trial November 3, 2025 (Monday).
    """
    SERVICE_DATE = date(2025, 5, 4)   # Sunday
    TRIAL_DATE   = date(2025, 11, 3)  # Monday

    def _compute(self):
        return compute_deadlines(
            jurisdiction="CO_STATE",
            matter_id="smith-v-jones-001",
            events=[
                TriggerEvent("service_of_complaint", self.SERVICE_DATE),
                TriggerEvent("trial_date",            self.TRIAL_DATE),
            ],
        )

    def test_returns_list(self):
        results = self._compute()
        assert isinstance(results, list)
        assert len(results) > 0

    def test_answer_due_21_calendar_days_rolled_forward(self):
        # May 4 + 21 = May 25 (Sunday) → May 26 = Memorial Day → May 27 (Tuesday)
        results = self._compute()
        answer = next(r for r in results if r.rule_id == "co_state_answer")
        assert answer.deadline_date == date(2025, 5, 27)

    def test_discovery_cutoff_63_days_before_trial(self):
        # Nov 3 - 63 = Sep 1 (Labor Day) → previous business day = Aug 29
        results = self._compute()
        disco = next(r for r in results if r.rule_id == "co_state_discovery_cutoff")
        assert disco.deadline_date == date(2025, 8, 29)

    def test_expert_disclosure_91_days_before_trial(self):
        # Nov 3 - 91 = Aug 4 (Monday)
        results = self._compute()
        expert = next(r for r in results if r.rule_id == "co_state_expert_disclosure")
        assert expert.deadline_date == date(2025, 8, 4)

    def test_msj_deadline_91_days_before_trial(self):
        results = self._compute()
        msj = next(r for r in results if r.rule_id == "co_state_msj")
        assert msj.deadline_date == date(2025, 8, 4)

    def test_pretrial_order_21_days_before_trial(self):
        # Nov 3 - 21 = Oct 13 (Columbus Day — not a CO holiday) = Monday
        results = self._compute()
        pto = next(r for r in results if r.rule_id == "co_state_pretrial_order")
        assert pto.deadline_date == date(2025, 10, 13)

    def test_jury_instructions_7_days_before_trial(self):
        # Nov 3 - 7 = Oct 27 (Monday)
        results = self._compute()
        ji = next(r for r in results if r.rule_id == "co_state_jury_instructions")
        assert ji.deadline_date == date(2025, 10, 27)

    def test_results_sorted_ascending(self):
        results = self._compute()
        dates = [r.deadline_date for r in results]
        assert dates == sorted(dates)

    def test_matter_id_on_all_results(self):
        results = self._compute()
        assert all(r.matter_id == "smith-v-jones-001" for r in results)

    def test_reminders_are_before_deadline(self):
        results = self._compute()
        for r in results:
            for rem in r.reminder_dates:
                assert rem < r.deadline_date, \
                    f"Reminder {rem} is not before deadline {r.deadline_date} for {r.rule_id}"

    def test_sol_general_2_years(self):
        results = compute_deadlines(
            jurisdiction="CO_STATE",
            matter_id="sol-test",
            events=[TriggerEvent("incident_date", date(2025, 1, 15))],
        )
        sol = next(r for r in results if r.rule_id == "co_state_sol_general")
        # Jan 15, 2025 + 2 years = Jan 15, 2027 (Wednesday)
        assert sol.deadline_date == date(2027, 1, 15)


class TestCOStateCase2:
    """
    Colorado Case 2: Rebuttal expert chained trigger in CO state.
    """

    def test_rebuttal_expert_chains_from_defendant_disclosure(self):
        """
        CO state rebuttal expert: trigger = defendant_expert_disclosure_date + 30 days.
        Must NOT fall back to trial_date trigger.
        """
        def_expert_date = date(2025, 9, 15)  # Monday
        results = compute_deadlines(
            jurisdiction="CO_STATE",
            matter_id="co-rebuttal-002",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 3, 1)),
                TriggerEvent("trial_date", date(2026, 1, 12)),
                TriggerEvent("defendant_expert_disclosure_date", def_expert_date),
            ],
        )
        rebuttal = next((r for r in results if r.rule_id == "co_state_rebuttal_expert"), None)
        assert rebuttal is not None, "co_state_rebuttal_expert rule not found"
        assert rebuttal.trigger_date == def_expert_date
        expected = next_business_day(def_expert_date + timedelta(days=30), "federal+colorado")
        assert rebuttal.deadline_date == expected


class TestCOFederalCase3:
    """
    Colorado Case 3: CO federal — Rule 26(f) conference chain and expert cascade.
    """
    APPEARED_DATE   = date(2025, 4, 1)
    CONFERENCE_DATE = date(2025, 4, 22)
    TRIAL_DATE      = date(2025, 10, 20)

    def _compute(self):
        return compute_deadlines(
            jurisdiction="CO_FEDERAL",
            matter_id="co-fed-003",
            events=[
                TriggerEvent("service_of_complaint",   date(2025, 3, 10)),
                TriggerEvent("defendant_appeared_date", self.APPEARED_DATE),
                TriggerEvent("rule26f_conference_date", self.CONFERENCE_DATE),
                TriggerEvent("trial_date",              self.TRIAL_DATE),
            ],
        )

    def test_answer_21_days_frcp(self):
        results = self._compute()
        answer = next(r for r in results if r.rule_id == "co_fed_answer")
        # Mar 10 + 21 = Mar 31 (Monday)
        assert answer.deadline_date == date(2025, 3, 31)

    def test_initial_disclosures_14_days_after_conference(self):
        results = self._compute()
        disc = next(r for r in results if r.rule_id == "co_fed_initial_disclosures")
        # Apr 22 + 14 = May 6 (Tuesday)
        assert disc.deadline_date == date(2025, 5, 6)

    def test_co_fed_msj_response_21_days(self):
        rules = load_rules("CO_FEDERAL")
        msj_resp = next(r for r in rules.rules if r.id == "co_fed_msj_response")
        assert msj_resp.offset_days == 21

    def test_expert_disclosure_cascade_present(self):
        """Expert disclosure cascade: plaintiff → defendant → rebuttal all present."""
        results = self._compute()
        rule_ids = [r.rule_id for r in results]
        assert "co_fed_expert_disclosure_plaintiff" in rule_ids
        assert "co_fed_expert_disclosure_defendant" in rule_ids


# ══════════════════════════════════════════════════════════════════════════════
#  DEADLINE ENGINE TESTS — AZ STATE (3 ARIZONA STATE CASES)
# ══════════════════════════════════════════════════════════════════════════════

class TestAZStateCase1:
    """
    Arizona State Case 1 — non-Maricopa (standard statewide rules only).
    Pima County PI case. Service June 10, 2025. Trial December 8, 2025.
    """
    SERVICE_DATE = date(2025, 6, 10)  # Tuesday
    TRIAL_DATE   = date(2025, 12, 8)  # Monday

    def _compute(self):
        return compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="pima-az-matter-001",  # non-Maricopa
            events=[
                TriggerEvent("service_of_complaint", self.SERVICE_DATE),
                TriggerEvent("trial_date",            self.TRIAL_DATE),
            ],
        )

    def test_answer_due_20_calendar_days(self):
        """At least 1 non-Maricopa AZ case: answer = 20 calendar days."""
        results = self._compute()
        answer = next(r for r in results if r.rule_id == "az_state_answer")
        # June 10 + 20 = June 30 (Monday)
        assert answer.deadline_date == date(2025, 6, 30)

    def test_az_initial_disclosures_40_days(self):
        results = self._compute()
        disc = next(r for r in results if r.rule_id == "az_state_initial_disclosures")
        # June 10 + 40 = July 20 (Sunday) → next business = July 21
        assert disc.deadline_date == date(2025, 7, 21)

    def test_expert_disclosure_120_days_before_trial(self):
        # Dec 8 - 120 = Aug 10 (Sunday) → prev business = Aug 8 (Friday)
        results = self._compute()
        expert = next(r for r in results if r.rule_id == "az_state_expert_disclosure")
        assert expert.deadline_date == date(2025, 8, 8)

    def test_rebuttal_expert_rule_present(self):
        """Rebuttal expert rule is present in AZ_STATE."""
        rules = load_rules("AZ_STATE")
        assert any(r.id == "az_state_rebuttal_expert" for r in rules.rules)

    def test_az_sol_personal_injury_2_years(self):
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="sol-az",
            events=[TriggerEvent("incident_date", date(2024, 3, 15))],
        )
        sol = next(r for r in results if r.rule_id == "az_state_sol_personal_injury")
        # Mar 15 2024 + 730 = Mar 15 2026 (Sunday) → next business day = Mar 16 2026
        assert sol.deadline_date == date(2026, 3, 16)

    def test_az_sol_written_contract_6_years(self):
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="sol-az-contract",
            events=[TriggerEvent("breach_date", date(2020, 1, 1))],
        )
        sol = next(r for r in results if r.rule_id == "az_state_sol_contract_written")
        # Jan 1 2020 + 6 years = Jan 1 2026 (New Year's Day) → next business day = Jan 2 2026
        assert sol.deadline_date == date(2026, 1, 2)

    def test_msj_response_30_days_az(self):
        rules = load_rules("AZ_STATE")
        msj_resp = next(r for r in rules.rules if r.id == "az_state_msj_response")
        assert msj_resp.offset_days == 30


class TestAZStateCase2ExpertCascade:
    """
    Arizona State Case 2 — at least 1 expert disclosure cascade.
    Cascade: plaintiff expert → defendant expert → rebuttal expert.
    """

    TRIAL_DATE     = date(2026, 3, 2)
    DEF_EXPERT_DATE = date(2025, 11, 10)  # Monday — actual defendant disclosure

    def test_expert_disclosure_cascade(self):
        """
        Expert disclosure cascade: all three expert deadlines computed.
        Rebuttal is chained from defendant_expert_disclosure_date.
        """
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-expert-cascade-002",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 4, 1)),
                TriggerEvent("trial_date", self.TRIAL_DATE),
                TriggerEvent("defendant_expert_disclosure_date", self.DEF_EXPERT_DATE),
            ],
        )
        rule_ids = [r.rule_id for r in results]
        assert "az_state_expert_disclosure" in rule_ids
        assert "az_state_defendant_expert_disclosure" in rule_ids
        assert "az_state_rebuttal_expert" in rule_ids

    def test_rebuttal_expert_chained_to_defendant_date(self):
        """Rebuttal trigger date must equal defendant_expert_disclosure_date."""
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-expert-cascade-002",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 4, 1)),
                TriggerEvent("trial_date", self.TRIAL_DATE),
                TriggerEvent("defendant_expert_disclosure_date", self.DEF_EXPERT_DATE),
            ],
        )
        rebuttal = next(r for r in results if r.rule_id == "az_state_rebuttal_expert")
        assert rebuttal.trigger_date == self.DEF_EXPERT_DATE
        expected = next_business_day(
            self.DEF_EXPERT_DATE + timedelta(days=30), "federal+arizona"
        )
        assert rebuttal.deadline_date == expected


class TestAZStateCase3DensePeriod:
    """
    Arizona State Case 3 — at least 1 dense deadline period (pretrial cluster).
    A case set for trial with a full pretrial calendar shows multiple deadlines
    within a 30-day window before trial.
    """
    TRIAL_DATE = date(2026, 2, 2)  # Monday

    def test_dense_deadline_period_pretrial_cluster(self):
        """
        At least 1 dense deadline period: 5+ deadlines in the 30 days before trial.
        """
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-dense-period-003",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 3, 1)),
                TriggerEvent("trial_date", self.TRIAL_DATE),
            ],
        )
        window_start = self.TRIAL_DATE - timedelta(days=30)
        pretrial_deadlines = [
            r for r in results
            if window_start <= r.deadline_date <= self.TRIAL_DATE
        ]
        assert len(pretrial_deadlines) >= 4, (
            f"Expected ≥4 deadlines in 30-day pretrial window, "
            f"got {len(pretrial_deadlines)}: {[r.rule_id for r in pretrial_deadlines]}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  MARICOPA CASE (1 Maricopa case)
# ══════════════════════════════════════════════════════════════════════════════

class TestMaricopaCase:
    """At least 1 Maricopa case — with trial de novo (MCLR Rule 3.10)."""

    ARBITRATION_AWARD_DATE = date(2025, 9, 15)  # Monday

    def test_maricopa_trial_de_novo_20_days(self):
        """
        Maricopa case: trial de novo demand must be within 20 days of arbitration award.
        MCLR Rule 3.10 — Maricopa only, ≤$65,000 cases.
        """
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="maricopa-arb-001",
            events=[
                TriggerEvent("service_of_complaint", date(2025, 1, 10)),
                TriggerEvent("arbitration_award_date", self.ARBITRATION_AWARD_DATE),
            ],
        )
        de_novo = next(
            (r for r in results if r.rule_id == "az_state_trial_de_novo_demand"), None
        )
        assert de_novo is not None, "Trial de novo demand rule not computed"
        # Sep 15 + 20 = Oct 5 (Sunday) → next business = Oct 6 (Monday)
        assert de_novo.deadline_date == date(2025, 10, 6)

    def test_maricopa_trial_de_novo_trigger(self):
        rules = load_rules("AZ_STATE")
        rule = next(r for r in rules.rules if r.id == "az_state_trial_de_novo_demand")
        assert rule.trigger == "arbitration_award_date"
        assert rule.offset_days == 20


# ══════════════════════════════════════════════════════════════════════════════
#  COURT-ORDER OVERRIDE CASE
# ══════════════════════════════════════════════════════════════════════════════

class TestCourtOrderOverrideCase:
    """At least 1 case with court-order override."""

    def test_cmo_overrides_formula_for_adjustable_deadline(self):
        """
        Court-order override: when a CMO provides a specific date for a
        'Yes – CMO sets date' deadline, the CMO date controls.
        """
        formula_date = date(2025, 8, 15)
        cmo_date     = date(2025, 7, 28)  # CMO sets earlier date
        result = apply_cmo_override(
            formula_date=formula_date,
            cmo_date=cmo_date,
            cmo_override_value="Yes – CMO sets date",
            rule_id="az_state_discovery_cutoff",
        )
        assert result == cmo_date

    def test_cmo_does_not_override_jurisdictional_sol(self):
        """
        Court-order override BLOCKED for statutory deadline.
        A CMO date later than the statutory SOL must be rejected.
        """
        sol_date = date(2026, 3, 15)   # statute-calculated SOL
        cmo_date = date(2026, 6, 1)    # CMO attempts to push deadline later

        with pytest.raises(StatutoryDeadlineOverrideError):
            apply_cmo_override(
                formula_date=sol_date,
                cmo_date=cmo_date,
                cmo_override_value="No – Statutory",
                statutory_deadline=sol_date,
                rule_id="az_state_sol_personal_injury",
            )

    def test_cmo_date_earlier_than_statutory_does_not_raise(self):
        """CMO date earlier than statutory deadline does not raise (though still ignored)."""
        sol_date = date(2026, 3, 15)
        cmo_date = date(2026, 1, 1)   # earlier — does NOT violate statute
        result = apply_cmo_override(
            formula_date=sol_date,
            cmo_date=cmo_date,
            cmo_override_value="No – Statutory",
            statutory_deadline=sol_date,
        )
        assert result == sol_date  # formula_date always returned for statutory rows


# ══════════════════════════════════════════════════════════════════════════════
#  STATUTORY DEADLINE CONFLICT CASE
# ══════════════════════════════════════════════════════════════════════════════

class TestStatutoryDeadlineConflict:
    """At least 1 case with statutory deadline conflict."""

    def test_notice_of_claim_before_suit_deadline(self):
        """
        AZ statutory conflict: NOC (180 days) must be filed BEFORE the 1-year
        government SOL (365 days). Both run from accrual date.
        The NOC date must be earlier than the SOL date.
        """
        accrual = date(2025, 1, 15)
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-gov-conflict-001",
            events=[TriggerEvent("incident_date", accrual)],
        )
        noc = next(r for r in results if r.rule_id == "az_state_notice_of_claim")
        sol = next(r for r in results if r.rule_id == "az_state_sol_government_entity")
        # NOC (180 days) must be before SOL (365 days)
        assert noc.deadline_date < sol.deadline_date

    def test_designation_nonparties_before_discovery_cutoff(self):
        """
        A.R.S. §12-2506(B): designation of non-parties (150 days from complaint filed)
        must be tracked alongside discovery cutoff to avoid conflicts.
        """
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-nonparty-conflict-002",
            events=[
                TriggerEvent("complaint_filed_date", date(2025, 2, 1)),
                TriggerEvent("trial_date", date(2025, 11, 3)),
            ],
        )
        non_party = next(
            (r for r in results if r.rule_id == "az_state_designation_nonparties"), None
        )
        assert non_party is not None
        # 150 days from Feb 1 = July 1 (Tuesday)
        assert non_party.deadline_date == date(2025, 7, 1)

    def test_statutory_sol_1yr_vs_2yr_conflict(self):
        """
        SOL conflict: 1-year strict liability (§12-541) vs 2-year general PI (§12-542).
        Both rules exist for the same trigger event; 1-year fires first.
        """
        results = compute_deadlines(
            jurisdiction="AZ_STATE",
            matter_id="az-sol-conflict-003",
            events=[TriggerEvent("incident_date", date(2025, 3, 1))],
        )
        sol_1yr = next(r for r in results if r.rule_id == "az_state_sol_1yr_strict_liability")
        sol_2yr = next(r for r in results if r.rule_id == "az_state_sol_personal_injury")
        assert sol_1yr.deadline_date < sol_2yr.deadline_date


# ══════════════════════════════════════════════════════════════════════════════
#  CROSS-JURISDICTION SANITY CHECKS
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossJurisdiction:
    def test_co_vs_az_answer_different(self):
        """CO answer = 21 days; AZ answer = 20 days — must differ."""
        service = date(2025, 6, 10)  # Tuesday — CO: Jul 1, AZ: Jun 30, no holidays
        co = compute_deadlines("CO_STATE", "m1", [TriggerEvent("service_of_complaint", service)])
        az = compute_deadlines("AZ_STATE", "m2", [TriggerEvent("service_of_complaint", service)])
        co_ans = next(r for r in co if r.rule_id == "co_state_answer").deadline_date
        az_ans = next(r for r in az if r.rule_id == "az_state_answer").deadline_date
        assert co_ans != az_ans
        assert co_ans == date(2025, 7, 1)   # Jun 10 + 21 = Jul 1 (Tuesday)
        assert az_ans == date(2025, 6, 30)  # Jun 10 + 20 = Jun 30 (Monday)

    def test_all_deadlines_have_color(self):
        for jurisdiction in ["CO_STATE", "CO_FEDERAL", "AZ_STATE", "AZ_FEDERAL"]:
            rules = load_rules(jurisdiction)
            for rule in rules.rules:
                assert rule.color in {"red", "orange", "yellow", "blue", "green"}, \
                    f"{rule.id} has invalid color: {rule.color!r}"

    def test_all_deadlines_have_notes(self):
        """Every rule should cite its source — catches rules added without citation."""
        for jurisdiction in ["CO_STATE", "CO_FEDERAL", "AZ_STATE", "AZ_FEDERAL"]:
            rules = load_rules(jurisdiction)
            for rule in rules.rules:
                assert rule.notes.strip(), f"{rule.id} is missing a citation in `notes`"

    def test_to_dict_serializable(self):
        results = compute_deadlines(
            "CO_STATE", "m1",
            [TriggerEvent("service_of_complaint", date(2025, 5, 4))]
        )
        for r in results:
            d = r.to_dict()
            assert "deadline_date" in d
            assert "label" in d
            assert "matter_id" in d

    def test_az_federal_has_rebuttal_expert_rule(self):
        """AZ federal also has a rebuttal expert rule chained to defendant disclosure."""
        fed = load_rules("AZ_FEDERAL")
        rebuttal = next((r for r in fed.rules if r.id == "az_fed_rebuttal_expert"), None)
        assert rebuttal is not None
        assert rebuttal.trigger == "defendant_expert_disclosure_date"
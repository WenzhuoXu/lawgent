"""compute_period: deterministic PRC period arithmetic (期间计算).

Holiday facts asserted here come from the State Council's 2026 notice
(国办发明电〔2025〕7号): 中秋 9/25–9/27, 国庆 10/1–10/7, with 9/20 (Sun) and
10/10 (Sat) worked; 春节 2/15–2/23 with 2/14 (Sat) and 2/28 (Sat) worked;
元旦 1/1–1/3 with 1/4 (Sun) worked. The 9/30 + 10 / + 7 cases are the
court-sms (cat-xierluo/legal-skills, MIT) reference examples.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("chinese_calendar")

from legal_helper.tools.dates import compute_period  # noqa: E402


def _run(start: str, period: str, rule: str = "prc_civil_procedure", **extra) -> dict:
    return json.loads(compute_period.call({"start": start, "period": period, "rule": rule, **extra}))


def test_schema_requires_start_and_period():
    schema = compute_period.input_schema
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"start", "period"}
    assert {"rule", "start_event"} <= set(schema["properties"])


# --- court-sms reference cases ------------------------------------------------


def test_court_sms_ten_days_ends_on_tiaoxiu_saturday_without_extension():
    # Served 9/30; day 10 is Sat 10/10, a 调休 working day — no extension.
    out = _run("2026-09-30", "10日")
    assert out["last_day"] == "2026-10-10"
    assert out["weekday"] == "Sat"
    assert "调休 working day" in out["steps"][-1]
    assert out["calendar"]["verified"] is True


def test_court_sms_seven_days_ends_in_national_day_and_moves_to_next_working_day():
    out = _run("2026-09-30", "7日")
    assert out["last_day"] == "2026-10-08"
    assert "2026-10-07" in out["steps"][-1] and "statutory holiday" in out["steps"][-1]


# --- appeal periods across the 2026 autumn holidays ------------------------------


def test_fifteen_day_appeal_ending_inside_national_day():
    # Served 9/16 → day 15 is 10/1 (National Day) → first working day 10/8.
    out = _run("2026-09-16", "15日", start_event="一审判决书送达")
    assert out["last_day"] == "2026-10-08"
    assert out["start_event"] == "一审判决书送达"


def test_fifteen_day_appeal_ending_in_mid_autumn_moves_only_past_that_holiday():
    # Served 9/10 → day 15 is Fri 9/25 (中秋); 9/26–27 are rest days; Mon 9/28 works.
    out = _run("2026-09-10", "15日")
    assert out["last_day"] == "2026-09-28"


def test_ordinary_weekend_moves_to_monday():
    out = _run("2026-03-06", "15日")
    assert out["last_day"] == "2026-03-23"
    assert "weekly rest day" in out["steps"][-1]


def test_tiaoxiu_saturday_and_sunday_are_working_days():
    assert _run("2026-01-30", "15日")["last_day"] == "2026-02-14"  # Sat, worked
    assert _run("2025-12-20", "15日")["last_day"] == "2026-01-04"  # Sun, worked


def test_holidays_inside_the_period_are_counted():
    # Day 1 is 10/1; holidays within the period count, only the last day moves.
    out = _run("2026-09-30", "15日")
    assert out["last_day"] == "2026-10-15"


# --- months and years (对应日 / 月末日) -------------------------------------------


def test_month_end_jan_31_plus_one_month_civil_code():
    # Feb 2026 has no 31st → 2/28 (月末日), itself a 调休 working Saturday.
    out = _run("2026-01-31", "1个月", rule="prc_civil_code")
    assert out["last_day"] == "2026-02-28"
    assert any("月末日" in step for step in out["steps"])
    assert any("第二百零二条" in b for b in out["basis"])


def test_month_end_on_a_sunday_then_moves():
    # 2025-10-31 + 1 month → 11/30 (Sun, month end) → Mon 12/1.
    out = _run("2025-10-31", "一个月", rule="prc_civil_code")
    assert out["last_day"] == "2025-12-01"


def test_leap_day_plus_one_year():
    out = _run("2024-02-29", "1年", rule="prc_civil_code")
    assert out["last_day"] == "2025-02-28"


def test_civil_procedure_month_period_notes_the_civil_code_method():
    out = _run("2026-01-15", "6个月")
    assert out["last_day"] == "2026-07-15"
    assert any("第二百零二条" in note for note in out["notes"])


# --- English and plain calendar ---------------------------------------------------


def test_english_calendar_case_makes_no_holiday_adjustment():
    out = _run("2026-01-31", "1 month", rule="calendar")
    assert out["last_day"] == "2026-02-28"
    assert "calendar" not in out  # no PRC holiday calendar consulted
    assert "no holiday or weekend adjustment" in out["caveats"][0]


def test_calendar_rule_keeps_a_weekend_last_day():
    out = _run("2026-03-06", "15 days", rule="calendar")
    assert out["last_day"] == "2026-03-21"


def test_english_period_under_prc_rule_matches_chinese():
    assert _run("2026-09-16", "15 days")["last_day"] == _run("2026-09-16", "十五日")["last_day"]


# --- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "period, expected",
    [("十日", "2026-03-16"), ("两年", "2028-03-06"), ("P10D", "2026-03-16"), ("2周", "2026-03-20")],
)
def test_period_forms(period, expected):
    assert _run("2026-03-06", period, rule="calendar")["last_day"] == expected


def test_chinese_date_input():
    assert _run("2026年9月30日", "7日")["last_day"] == "2026-10-08"


@pytest.mark.parametrize(
    "args",
    [
        {"start": "2026-09-30", "period": "48小时"},
        {"start": "2026-09-30", "period": "10日", "rule": "us_frcp"},
        {"start": "2026-02-30", "period": "10日"},
        {"start": "2026-09-30", "period": "0日"},
        {"start": "2026-09-30", "period": "soon"},
    ],
)
def test_errors_are_reported_not_guessed(args):
    out = json.loads(compute_period.call(args))
    assert "error" in out and "last_day" not in out


# --- what the arithmetic cannot know -------------------------------------------------


def test_civil_procedure_caveats_cover_service_proof_and_announcement():
    caveats = " ".join(_run("2026-09-30", "15日")["caveats"])
    assert "QR-code" in caveats and "SMS" in caveats
    assert "公告送达" in caveats and "第二百八十三条" in caveats
    assert "第二百八十六条" in caveats


def test_civil_code_caveats_cover_cutoff_and_tolling():
    caveats = " ".join(_run("2026-01-31", "3年", rule="prc_civil_code")["caveats"])
    assert "24:00" in caveats
    assert "除斥期间" in caveats


def test_year_outside_calendar_data_is_flagged_unverified():
    # Day 15 lands on 1 January of the first year the installed calendar does
    # not cover (2027 with chinesecalendar 1.11.0; the library is refreshed
    # yearly). Only weekends and the fixed-date holidays are known there, so
    # 元旦 still moves the last day, and the result says it is unverified.
    from chinese_calendar import constants

    first_uncovered = max(d.year for d in constants.holidays) + 1
    out = _run(f"{first_uncovered - 1}-12-17", "15日")
    assert out["calendar"]["verified"] is False
    assert out["caveats"][0].startswith("UNVERIFIED CALENDAR")
    assert out["last_day"] > f"{first_uncovered}-01-01"

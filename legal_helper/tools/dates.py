"""Deterministic legal period computation (期间计算).

``compute_period`` turns "trigger date + period" into the last day of the
period under the PRC counting rules and reports every adjustment with the
provision that makes it, so a deadline in an answer is arithmetic the model
can show rather than a date it recalled.

Rules, verified 2026-10-07 at the official texts:

- 民事诉讼法（2023修正）第八十五条: the start 时/日 is not counted (第二款); a
  last day that is a 法定休假日 moves to the first day after it (第三款); time
  in transit is not counted and a filing posted before expiry is timely
  (第四款). 民诉法解释（2022修正）第一百二十五条: day/month/year periods run
  from the next day.
- 民法典 第二百零一条至第二百零四条: start day excluded; a month/year period
  ends on the 对应日, or the month's last day when there is none; a last day
  on a 法定休假日 moves to the day after the holiday ends; cut-off 24:00 or the
  close of business; law or agreement may provide otherwise.
- 法定休假日 = statutory holidays plus the weekly rest days (《国务院关于职工
  工作时间的规定》第七条), as rearranged each year by the State Council's
  放假调休 notice — a 调休 Saturday or Sunday is a working day. The calendar
  comes from ``chinesecalendar`` (MIT), which carries those notices; for a
  year it does not cover, only weekends and the fixed-date holidays of
  《全国年节及纪念日放假办法》（2024修订）第二条 are known, and the result says so.

``cat-xierluo/legal-skills`` ``court-sms`` (MIT) is the PRC rule reference for
the service-evidence caveats and the 2026 调休 test cases.
"""

from __future__ import annotations

import calendar as _calendar
import datetime as _dt
import json
import re
from typing import Any, Optional

from anthropic import beta_tool

RULES = ("prc_civil_procedure", "prc_civil_code", "calendar")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# 《全国年节及纪念日放假办法》（2024修订）第二条 — the Gregorian-date holidays.
# The lunar ones (春节、清明、端午、中秋) and the yearly 调休 cannot be derived.
_FIXED_HOLIDAYS = {(1, 1): "元旦", (5, 1): "劳动节", (5, 2): "劳动节",
                   (10, 1): "国庆节", (10, 2): "国庆节", (10, 3): "国庆节"}

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# No single-letter aliases: "15m" could be minutes. ISO 8601 (P15D, P1M) is
# accepted separately.
_UNIT_ALIASES = {
    "日": "day", "天": "day", "day": "day", "days": "day",
    "周": "week", "星期": "week", "week": "week", "weeks": "week",
    "个月": "month", "月": "month", "month": "month", "months": "month",
    "年": "year", "year": "year", "years": "year",
    "小时": "hour", "时": "hour", "hour": "hour", "hours": "hour",
}

_BASIS = {
    "prc_civil_procedure": [
        "民事诉讼法（2023修正）第八十五条第二款：期间开始的时和日，不计算在期间内",
        "民事诉讼法（2023修正）第八十五条第三款：期间届满的最后一日是法定休假日的，"
        "以法定休假日后的第一日为期间届满的日期",
        "民事诉讼法（2023修正）第八十五条第四款：期间不包括在途时间，诉讼文书在期满前交邮的，不算过期",
        "民诉法解释（2022修正）第一百二十五条：以日、月、年计算的期间从次日起算",
    ],
    "prc_civil_code": [
        "民法典 第二百零一条第一款：按照年、月、日计算期间的，开始的当日不计入，自下一日开始计算",
        "民法典 第二百零二条：按照年、月计算期间的，到期月的对应日为期间的最后一日；"
        "没有对应日的，月末日为期间的最后一日",
        "民法典 第二百零三条：期间的最后一日是法定休假日的，以法定休假日结束的次日为期间的最后一日；"
        "截止时间为二十四时，有业务时间的，停止业务活动的时间为截止时间",
        "民法典 第二百零四条：法律另有规定或者当事人另有约定的除外",
    ],
    "calendar": [
        "Plain calendar arithmetic: the start day is not counted; month/year periods end on "
        "the same day-number, or the month's last day when there is none; no holiday roll",
    ],
}

_HOLIDAY_BASIS = (
    "法定休假日: statutory holidays and weekly rest days (《国务院关于职工工作时间的规定》第七条) "
    "as rearranged by the State Council's annual 放假调休 notice; a 调休 working day is not a rest day"
)

_CAVEATS = {
    "prc_civil_procedure": [
        "start must be this recipient's legally effective service date for this document: "
        "送达回证 signature, the court system's 发送成功 record (民诉法解释 第一百三十五条; later "
        "arrival can be proved), or the postal receipt date. A QR-code time, the document's "
        "own date, a download time or an ordinary SMS timestamp does not prove service.",
        "公告送达 is deemed served 30 days after the notice (民事诉讼法 第九十五条), 60 days for a "
        "party without a PRC domicile (第二百八十三条) — count from that date.",
        "confirm the period applies: appeal 15 days (judgment) / 10 days (appealable ruling), "
        "第一百七十一条; 30 days without a PRC domicile, 第二百八十六条; none for 小额诉讼 or other "
        "final decisions; each party's period runs from its own service (民诉法解释 第二百四十四条).",
        "a filing posted before expiry is timely (第八十五条第四款); a period missed for good "
        "reason may be extended on application within 10 days after the obstacle ends (第八十六条).",
    ],
    "prc_civil_code": [
        "cut-off is 24:00 on the last day, or the close of business where business hours apply "
        "(民法典 第二百零三条第二款); law or agreement may count differently (第二百零四条).",
        "arithmetic only: a 诉讼时效 can be suspended or interrupted (第一百九十四条、第一百九十五条), "
        "a 除斥期间 cannot (第一百九十九条); check the start event and any special-law period.",
    ],
    "calendar": [
        "no holiday or weekend adjustment was made; apply the governing rule (statute, court "
        "rules or contract) if the last day is a non-business day.",
    ],
}


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


def _parse_cn_number(text: str) -> Optional[int]:
    """Parse a Chinese numeral up to the thousands (e.g. 十五, 一百八十, 两)."""
    if not text or any(ch not in _CN_DIGITS and ch not in _CN_UNITS for ch in text):
        return None
    total, current = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        else:
            total += (current or 1) * _CN_UNITS[ch]
            current = 0
    return total + current


def _parse_period(period: str) -> tuple[int, str] | str:
    """Return (amount, unit) with unit in day/week/month/year/hour, or an error."""
    raw = (period or "").strip()
    if not raw:
        return "period is required, e.g. '15日', '一个月', '2年', '15 days', '1 month'"
    iso = re.fullmatch(r"[Pp](\d+)([DWMYdwmy])", raw)
    if iso:
        return int(iso.group(1)), {"d": "day", "w": "week", "m": "month", "y": "year"}[iso.group(2).lower()]
    m = re.fullmatch(r"(\d+|[零〇一二两三四五六七八九十百千]+)\s*(个月|小时|星期|[日天周月年时]|[A-Za-z]+)", raw)
    if not m:
        return f"cannot read period {raw!r}; use e.g. '15日', '十日', '3个月', '2年', '15 days'"
    number, unit = m.group(1), m.group(2).lower()
    amount = int(number) if number.isdigit() else _parse_cn_number(number)
    if amount is None:
        return f"cannot read the number in {raw!r}"
    unit_key = _UNIT_ALIASES.get(unit)
    if unit_key is None:
        return f"unknown unit {m.group(2)!r} in {raw!r}"
    if amount <= 0:
        return "period must be positive"
    return amount, unit_key


def _parse_date(text: str) -> _dt.date | str:
    raw = (text or "").strip()
    m = re.fullmatch(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw)
    if not m:
        return f"cannot read date {raw!r}; use YYYY-MM-DD (e.g. 2026-09-30)"
    try:
        return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        return f"invalid date {raw!r}: {exc}"


def _add_months(start: _dt.date, months: int) -> tuple[_dt.date, bool]:
    """Same day-number in the target month, else that month's last day."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    last = _calendar.monthrange(year, month)[1]
    if start.day <= last:
        return _dt.date(year, month, start.day), False
    return _dt.date(year, month, last), True


def _day_status(day: _dt.date) -> dict[str, Any]:
    """Classify a day: rest or working, why, and whether the data is official."""
    try:
        import chinese_calendar as cc  # chinesecalendar (MIT)
    except ImportError:  # pragma: no cover — dependency declared in requirements
        cc = None
    if cc is not None:
        try:
            is_holiday, name = cc.get_holiday_detail(day)
        except NotImplementedError:
            pass
        else:
            if not is_holiday:
                kind = "调休 working day" if name else "working day"
                return {"rest": False, "kind": kind, "name": name, "verified": True}
            kind = "statutory holiday" if name else "weekly rest day"
            return {"rest": True, "kind": kind, "name": name, "verified": True}
    fixed = _FIXED_HOLIDAYS.get((day.month, day.day))
    if fixed:
        return {"rest": True, "kind": "statutory holiday", "name": fixed, "verified": False}
    if day.weekday() >= 5:
        return {"rest": True, "kind": "weekly rest day", "name": None, "verified": False}
    return {"rest": False, "kind": "working day", "name": None, "verified": False}


def _calendar_source() -> dict[str, Any]:
    try:
        import chinese_calendar as cc
        from chinese_calendar import constants
    except ImportError:  # pragma: no cover
        return {"source": "none (chinesecalendar not installed)", "covers": None}
    years = sorted({d.year for d in constants.holidays})
    version = getattr(cc, "__version__", "unknown")
    return {
        "source": f"chinesecalendar {version} (State Council 放假调休 notices)",
        "covers": f"{years[0]}-{years[-1]}" if years else None,
    }


def _describe(day: _dt.date, status: dict[str, Any]) -> str:
    label = status["kind"] + (f" ({status['name']})" if status.get("name") else "")
    return f"{day.isoformat()} ({_WEEKDAYS[day.weekday()]}) is a {label}"


@beta_tool
def compute_period(
    start: str,
    period: str,
    rule: str = "prc_civil_procedure",
    start_event: str = "",
) -> str:
    """Compute the last day of a legal period (期间计算) deterministically.

    Use this for every deadline, limitation expiry or 期限 stated in an
    answer instead of counting dates yourself. It excludes the start day,
    applies the 对应日 / month-end rule for months and years, and moves a
    last day that falls on a 法定休假日 to the next working day using the
    official holiday and 调休 calendar (a 调休 Saturday is a working day).
    The result lists each step with its legal basis and the facts the
    arithmetic cannot know (proof of the service date, 公告送达, whether the
    period applies at all).

    Args:
        start: The day the triggering event occurred, YYYY-MM-DD — e.g. the
            date the judgment was served (送达之日). This day is not counted.
        period: Length such as "15日", "十日", "3个月", "2年", "15 days",
            "1 month", "2 years" (weeks are converted to days; hour-based
            periods are not supported).
        rule: "prc_civil_procedure" (民事诉讼法 第八十五条 — court and
            procedural periods), "prc_civil_code" (民法典 第二百零一条至第二百零四条
            — limitation and other civil periods), or "calendar" (plain
            arithmetic, no holiday roll — e.g. foreign or contractual periods).
        start_event: Optional label for what happened on the start date
            (e.g. "判决书送达"), echoed back with the result.
    """
    rule_key = (rule or "").strip().lower()
    if rule_key not in RULES:
        return _error(f"unknown rule {rule!r}", rules=list(RULES))
    start_date = _parse_date(start)
    if isinstance(start_date, str):
        return _error(start_date)
    parsed = _parse_period(period)
    if isinstance(parsed, str):
        return _error(parsed)
    amount, unit = parsed
    if unit == "hour":
        return _error(
            "hour-based periods are not supported: they run from the exact time "
            "(民事诉讼法 第八十五条, 民法典 第二百零一条第二款) — compute from the timestamp"
        )

    steps: list[str] = []
    notes: list[str] = []
    steps.append(
        f"start day {start_date.isoformat()} is not counted; day 1 is "
        f"{(start_date + _dt.timedelta(days=1)).isoformat()}"
    )
    if unit == "week":
        notes.append(f"{amount} week(s) counted as {amount * 7} days")
        amount, unit = amount * 7, "day"
    if unit == "day":
        nominal = start_date + _dt.timedelta(days=amount)
        steps.append(f"day {amount} is {nominal.isoformat()} ({_WEEKDAYS[nominal.weekday()]})")
    else:
        months = amount * (12 if unit == "year" else 1)
        nominal, clamped = _add_months(start_date, months)
        if clamped:
            steps.append(
                f"{nominal.strftime('%Y-%m')} has no day {start_date.day}; the month's last day "
                f"{nominal.isoformat()} ({_WEEKDAYS[nominal.weekday()]}) is the last day (月末日)"
            )
        else:
            steps.append(
                f"corresponding day (对应日) is {nominal.isoformat()} "
                f"({_WEEKDAYS[nominal.weekday()]})"
            )
        if rule_key == "prc_civil_procedure":
            notes.append(
                "民事诉讼法 sets the units and the start-day rule but no month-end rule; "
                "the 对应日 / 月末日 method of 民法典 第二百零二条 is applied"
            )

    last = nominal
    verified = True
    caveats = list(_CAVEATS[rule_key])
    if rule_key != "calendar":
        status = _day_status(last)
        verified = verified and status["verified"]
        if not status["rest"]:
            steps.append(_describe(last, status) + "; no adjustment")
        else:
            first = last
            while status["rest"]:
                last += _dt.timedelta(days=1)
                status = _day_status(last)
                verified = verified and status["verified"]
            steps.append(
                _describe(first, _day_status(first))
                + f"; the last day moves to {last.isoformat()} ({_WEEKDAYS[last.weekday()]}), "
                + "the first working day after the 法定休假日"
            )
        if not verified:
            years = sorted({nominal.year, last.year})
            caveats.insert(0, (
                f"UNVERIFIED CALENDAR: holiday data for {', '.join(map(str, years))} is not in "
                "the installed calendar, so only weekends and the fixed-date holidays (元旦, 劳动节 "
                "5/1–5/2, 国庆节 10/1–10/3) were treated as rest days; lunar holidays and 调休 are "
                "unknown. Check the State Council's 放假调休 notice for that year."
            ))
    payload: dict[str, Any] = {
        "last_day": last.isoformat(),
        "weekday": _WEEKDAYS[last.weekday()],
        "start": start_date.isoformat(),
        "period": {"input": period, "amount": parsed[0], "unit": parsed[1]},
        "rule": rule_key,
        "steps": steps,
        "basis": list(_BASIS[rule_key]) + ([_HOLIDAY_BASIS] if rule_key != "calendar" else []),
        "caveats": caveats,
    }
    if rule_key != "calendar":
        payload["calendar"] = {**_calendar_source(), "verified": verified}
    if notes:
        payload["notes"] = notes
    if start_event:
        payload["start_event"] = start_event
    return json.dumps(payload, ensure_ascii=False)


__all__ = ["compute_period", "RULES"]

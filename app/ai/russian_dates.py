from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTH_PATTERNS = (
    (1, r"\bянвар[ьяе]?\b"),
    (2, r"\bфеврал[ьяе]?\b"),
    (3, r"\bмарт(?:а|е)?\b"),
    (4, r"\bапрел[ьяе]?\b"),
    (5, r"\bма(?:й|я|е)\b"),
    (6, r"\bиюн[ьяе]?\b"),
    (7, r"\bиюл[ьяе]?\b"),
    (8, r"\bавгуст(?:а|е)?\b"),
    (9, r"\bсентябр[ьяе]?\b"),
    (10, r"\bоктябр[ьяе]?\b"),
    (11, r"\bноябр[ьяе]?\b"),
    (12, r"\bдекабр[ьяе]?\b"),
)

CARDINAL_NUMBERS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
}

DAY_ORDINALS = {
    "первого": 1,
    "второго": 2,
    "третьего": 3,
    "четвертого": 4,
    "пятого": 5,
    "шестого": 6,
    "седьмого": 7,
    "восьмого": 8,
    "девятого": 9,
    "десятого": 10,
    "одиннадцатого": 11,
    "двенадцатого": 12,
    "тринадцатого": 13,
    "четырнадцатого": 14,
    "пятнадцатого": 15,
    "шестнадцатого": 16,
    "семнадцатого": 17,
    "восемнадцатого": 18,
    "девятнадцатого": 19,
    "двадцатого": 20,
    "двадцать первого": 21,
    "двадцать второго": 22,
    "двадцать третьего": 23,
    "двадцать четвертого": 24,
    "двадцать пятого": 25,
    "двадцать шестого": 26,
    "двадцать седьмого": 27,
    "двадцать восьмого": 28,
    "двадцать девятого": 29,
    "тридцатого": 30,
    "тридцать первого": 31,
}

_DAY_TOKEN = "|".join(
    [r"\d{1,2}", *(re.escape(value) for value in sorted(DAY_ORDINALS, key=len, reverse=True))]
)


@dataclass(frozen=True)
class ParsedRussianDate:
    value: date
    span: tuple[int, int]
    source: str


def normalized_russian_text(value: str) -> str:
    return value.casefold().replace("ё", "е")


def _day_number(token: str) -> int:
    normalized = normalized_russian_text(token)
    return int(normalized) if normalized.isdigit() else DAY_ORDINALS[normalized]


def parse_russian_day_expression(text: str, *, today: date) -> ParsedRussianDate | None:
    """Parse an explicit Russian day-of-month expression as a past/current date.

    A bare ``N числа`` is accepted only when that day has already occurred in the
    current month. A future bare day is ambiguous and is deliberately rejected.
    A month without a year means its most recent occurrence.
    """
    normalized = normalized_russian_text(text)
    for month, month_pattern in MONTH_PATTERNS:
        match = re.search(
            rf"(?<!\w)(?P<day>{_DAY_TOKEN})\s+(?:числа\s+)?{month_pattern}"
            rf"(?:\s+(?P<year>20\d{{2}}))?(?!\d)",
            normalized,
        )
        if match is None:
            continue
        day = _day_number(match.group("day"))
        year = int(match.group("year")) if match.group("year") else today.year
        try:
            value = date(year, month, day)
        except ValueError as exc:
            raise ValueError("invalid day-of-month") from exc
        if match.group("year") is None and value > today:
            try:
                value = date(year - 1, month, day)
            except ValueError as exc:
                raise ValueError("invalid day-of-month") from exc
        return ParsedRussianDate(value=value, span=match.span(), source="day_month")

    match = re.search(rf"(?<!\w)(?P<day>{_DAY_TOKEN})\s+числа(?!\w)", normalized)
    if match is None:
        return None
    day = _day_number(match.group("day"))
    if day > today.day:
        raise ValueError("ambiguous future day-of-month")
    try:
        value = date(today.year, today.month, day)
    except ValueError as exc:
        raise ValueError("invalid day-of-month") from exc
    return ParsedRussianDate(value=value, span=match.span(), source="day_of_month")


def parse_future_russian_day_expression(text: str, *, today: date) -> ParsedRussianDate | None:
    """Parse an explicit month date, choosing its next occurrence when year is omitted."""
    normalized = normalized_russian_text(text)
    for month, month_pattern in MONTH_PATTERNS:
        match = re.search(
            rf"(?<!\w)(?P<day>{_DAY_TOKEN})\s+{month_pattern}(?:\s+(?P<year>20\d{{2}}))?(?!\d)",
            normalized,
        )
        if match is None:
            continue
        day = _day_number(match.group("day"))
        year = int(match.group("year")) if match.group("year") else today.year
        try:
            value = date(year, month, day)
        except ValueError as exc:
            raise ValueError("invalid day-of-month") from exc
        if match.group("year") is None and value <= today:
            value = date(year + 1, month, day)
        return ParsedRussianDate(value=value, span=match.span(), source="day_month")
    return None


def contains_unparsed_date_expression(text: str) -> bool:
    normalized = normalized_russian_text(text)
    if re.search(r"\b(?:понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресень[ея])\b", normalized):
        return True
    if re.search(r"\bчисла\b", normalized):
        return True
    if re.search(
        r"\b(?:в|за|на)\s+(?:(?:прошл|следующ|эт)\w*\s+)?(?:недел\w*|выходн\w*|начал\w*|конц\w*)\b",
        normalized,
    ):
        return True
    return any(re.search(pattern, normalized) for _month, pattern in MONTH_PATTERNS)


def parse_bounded_cardinal(token: str, *, maximum: int) -> int | None:
    normalized = normalized_russian_text(token).strip()
    value = int(normalized) if normalized.isdigit() else CARDINAL_NUMBERS.get(normalized)
    return value if value is not None and 1 <= value <= maximum else None

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache


def _easter_sunday(year: int) -> date:
    # Algoritmo de Meeus/Jones/Butcher (calendário gregoriano)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


@lru_cache(maxsize=32)
def portuguese_national_holidays(year: int) -> dict[date, str]:
    easter = _easter_sunday(year)
    good_friday = easter - timedelta(days=2)
    corpus_christi = easter + timedelta(days=60)

    holidays = {
        date(year, 1, 1): "Ano Novo",
        good_friday: "Sexta-feira Santa",
        easter: "Domingo de Páscoa",
        date(year, 4, 25): "Dia da Liberdade",
        date(year, 5, 1): "Dia do Trabalhador",
        corpus_christi: "Corpo de Deus",
        date(year, 6, 10): "Dia de Portugal",
        date(year, 8, 15): "Assunção de Nossa Senhora",
        date(year, 10, 5): "Implantação da República",
        date(year, 11, 1): "Dia de Todos os Santos",
        date(year, 12, 1): "Restauração da Independência",
        date(year, 12, 8): "Imaculada Conceição",
        date(year, 12, 25): "Natal",
    }
    return holidays


def get_portuguese_holiday_name(date_obj: date) -> str:
    return portuguese_national_holidays(date_obj.year).get(date_obj, "")


def is_portuguese_holiday(date_obj: date) -> bool:
    return date_obj in portuguese_national_holidays(date_obj.year)


def iter_portuguese_holidays(start_date: date, end_date: date) -> list[tuple[date, str]]:
    if end_date < start_date:
        return []
    result: list[tuple[date, str]] = []
    for year in range(start_date.year, end_date.year + 1):
        for day, name in portuguese_national_holidays(year).items():
            if start_date <= day <= end_date:
                result.append((day, name))
    result.sort(key=lambda item: item[0])
    return result

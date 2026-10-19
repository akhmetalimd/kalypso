"""Формулы индексов нозологий на общей основе (registry_core), без БД."""
import pytest

from registry_core import mipi_c, smipi


# --- MCL ---------------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((49, 1, "lt_067", 6.6), 0, "low"),
    ((50, 1, "067_099", 6.7), 3, "low"),            # 50 лет — 1, ЛДГ 0,67–0,99 — 1, лейкоциты 6,7 — 1
    ((60, 2, "lt_067", 5.0), 4, "intermediate"),    # 60 лет — 2, ECOG 2 — 2
    ((69, 1, "100_149", 9.99), 5, "intermediate"),
    ((70, 1, "100_149", 10.0), 7, "high"),
    ((70, 3, "ge_150", 15.0), 11, "high"),
])
def test_smipi(args, score, group):
    r = smipi(*args)
    assert (r["score"], r["group"]) == (score, group)


@pytest.mark.parametrize("missing", range(4))
def test_smipi_needs_all(missing):
    args = [70, 2, "ge_150", 15.0]
    args[missing] = None
    assert smipi(*args) is None
    assert smipi(70, 2, "not_done", 15.0) is None


@pytest.mark.parametrize("group, ki67, expected", [
    ("low", False, "low"), ("low", True, "low_intermediate"), ("intermediate", False, "low_intermediate"),
    ("intermediate", True, "high_intermediate"), ("high", False, "high_intermediate"), ("high", True, "high"),
])
def test_mipi_c(group, ki67, expected):
    assert mipi_c(group, ki67)["group"] == expected
    assert mipi_c(group, None) is None and mipi_c(None, True) is None

"""Формулы индексов нозологий на общей основе (registry_core), без БД."""
import pytest

from registry_core import ghsg_group, ips, mipi_c, smipi


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


# --- лимфома Ходжкина ------------------------------------------------------------
@pytest.mark.parametrize("args, group", [
    (("I", False, False, False, False, False), "early_favorable"),
    (("II", False, False, False, True, False), "early_unfavorable"),       # только СОЭ
    (("IE", False, False, None, False, False), "early_unfavorable"),       # E из стадии
    (("II", True, True, False, False, False), "advanced"),                 # IIB + массивное средостение
    (("II", False, True, False, False, False), "early_unfavorable"),       # IIA + массивное средостение
    (("II", True, False, False, True, True), "early_unfavorable"),         # IIB + СОЭ, ≥ 3 зон (c, d)
    (("III", None, None, None, None, None), "advanced"),
])
def test_ghsg(args, group):
    assert ghsg_group(*args)["group"] == group


def test_ghsg_needs_data():
    assert ghsg_group("II", None, True, False, False, False) is None      # IIA или IIB — неизвестно
    assert ghsg_group("I", False, None, False, False, False) is None
    assert ghsg_group(None, False, False, False, False, False) is None


@pytest.mark.parametrize("args, score, group", [
    ((45, 140, False, 30, False, 7, 2.0), 0, "low"),
    ((39, 140, True, 50, False, 7, 2.0), 3, "intermediate"),              # альбумин, пол, возраст
    ((39, 104, True, 50, True, 15, 0.59), 7, "high"),
    ((45, 140, False, 30, False, 20, 1.5), 2, "intermediate"),            # лейкоциты ≥ 15, лимфоциты 7,5 % < 8 %
])
def test_ips(args, score, group):
    r = ips(*args)
    assert (r["score"], r["group"]) == (score, group)
    assert ips(None, 140, False, 30, False, 7, 2.0) is None


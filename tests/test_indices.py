"""Формулы индексов нозологий на общей основе (registry_core), без БД."""
import pytest

from registry_core import (aa_ipi, cns_ipi, ghsg_group, index_group, index_groups, ips, ipss_wm, malt_ipi, mipi_c, pink, pit,
                           ripss_wm, smipi)


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


# --- Вальденстрём ----------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((60, 130, 200, 2.0, 30), 0, "low"),
    ((60, 110, 200, 2.0, 30), 1, "low"),             # один фактор, не возраст
    ((70, 130, 200, 2.0, 30), 1, "intermediate"),    # один фактор — возраст > 65
    ((60, 110, 90, 2.0, 30), 2, "intermediate"),
    ((60, 115, 100, 3.1, 30), 3, "high"),            # Hb ≤ 115 и тромбоциты ≤ 100 включительно
    ((70, 110, 90, 4.0, 75), 5, "high"),
])
def test_ipss_wm(args, score, group):
    r = ipss_wm(*args)
    assert (r["score"], r["group"]) == (score, group)
    assert index_groups("IPSS-WM", 1) == ("low", "intermediate") and index_group("IPSS-WM", 1) is None
    assert index_group("IPSS-WM", 2) == "intermediate"


@pytest.mark.parametrize("args, score, group", [
    ((65, 3.9, 249, 35), 0, "very_low"),
    ((66, 3.9, 249, 35), 1, "low"),
    ((76, 3.9, 249, 35), 2, "intermediate"),
    ((76, 4.0, 249, 35), 3, "high"),
    ((76, 4.0, 250, 34.9), 5, "very_high"),
])
def test_ripss_wm(args, score, group):
    r = ripss_wm(*args)
    assert (r["score"], r["group"]) == (score, group)


# --- ЛМЗ -------------------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((69, False, "le_uln"), 0, "low"), ((70, False, "le_uln"), 1, "intermediate"),
    ((70, True, "le_uln"), 2, "high"), ((70, True, "gt_3_uln"), 3, "high"),
])
def test_malt_ipi(args, score, group):
    r = malt_ipi(*args)
    assert (r["score"], r["group"]) == (score, group)
    assert malt_ipi(70, None, "le_uln") is None


# --- ПТКЛ ------------------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((60, 1, "le_uln", False), 0, "low"), ((61, 1, "le_uln", False), 1, "low_intermediate"),
    ((61, 2, "le_uln", False), 2, "high_intermediate"), ((61, 2, "uln_1_3", False), 3, "high"),
    ((61, 2, "uln_1_3", True), 4, "high"),
])
def test_pit(args, score, group):
    r = pit(*args)
    assert (r["score"], r["group"]) == (score, group)


@pytest.mark.parametrize("args, group", [
    ((60, False, False, False), "low"), ((61, False, False, False), "intermediate"),
    ((61, True, False, False), "high"), ((40, True, True, True), "high"),
])
def test_pink(args, group):
    assert pink(*args)["group"] == group
    assert pink(None, True, True, True) is None


# --- HGBL: CNS-IPI ---------------------------------------------------------------
@pytest.mark.parametrize("args, score, group", [
    ((50, 0, False, "le_uln", False, False), 0, "low"),
    ((61, 2, False, "le_uln", False, True), 3, "intermediate"),
    ((61, 2, True, "uln_1_3", False, False), 4, "high"),
])
def test_cns_ipi(args, score, group):
    r = cns_ipi(*args)
    assert (r["score"], r["group"]) == (score, group)
    assert cns_ipi(61, 2, True, "uln_1_3", False, None) is None


# --- ПМБКЛ: aaIPI ----------------------------------------------------------------
@pytest.mark.parametrize("args, group", [
    ((1, False, "le_uln"), "low"), ((2, False, "le_uln"), "low_intermediate"),
    ((2, True, "le_uln"), "high_intermediate"), ((2, True, "gt_3_uln"), "high"),
])
def test_aa_ipi(args, group):
    assert aa_ipi(*args)["group"] == group
    assert aa_ipi(None, True, "le_uln") is None


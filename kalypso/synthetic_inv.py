"""Синтетические исследования (ИФТ, генетика, вирусы, аутоиммунный скрининг) — не у всех пациентов.

Записи вносятся через сервис карточки. Генетика согласована с полями модуля (вывод для
индекса = результат исследования); вирусный статус заменяет прежние вирусные поля модулей
(ВИЧ у ЛБ, HCV у ЛМЗ, ДНК EBV у ПТКЛ — с той же частотой положительных).
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import registry_core as core
from kalypso import investigations as inv
from kalypso import registry
from kalypso.rbac import Principal

# Типичный фенотип клона по нозологиям — как пишут в заключении ИФТ.
FLOW = {
    "cll_sll": ("CD5+ CD19+ CD20 dim CD23+ CD200+ CD10− FMC7− CD79b dim", "blood", 0.85),
    "mcl": ("CD5+ CD19+ CD20+ CD23− CD200− CD10− FMC7+", "blood", 0.6),
    "hcl": ("CD19+ CD20 bright CD11c+ CD25+ CD103+ CD123+ CD5− CD10−", "blood", 0.9),
    "tlgll": ("CD3+ CD8+ CD57+ CD16+ CD56− CD4− CD5 dim CD7 dim", "blood", 0.9),
    "waldenstrom": ("CD19+ CD20+ CD22+ CD25+ CD5− CD10− CD23− CD138+", "bone_marrow", 0.5),
    "mzl": ("CD19+ CD20+ CD5− CD10− CD23− CD103− CD11c+", "blood", 0.4),
    "follicular": ("CD19+ CD20+ CD10+ CD5− CD23−", "lymph_node", 0.3),
    "dlbcl": ("CD19+ CD20+ CD10+ CD5−", "lymph_node", 0.15),
    "burkitt": ("CD19+ CD20+ CD10+ CD38 bright CD5− TdT−", "bone_marrow", 0.5),
    "ptcl": ("CD3+ CD4+ CD8− CD7− CD5 dim CD30 частично", "lymph_node", 0.3),
    "mf_ss": ("CD3+ CD4+ CD7− CD26− CD8−", "blood", 0.4),
}
# Положительный результат вирусного теста: базовая частота и особые для нозологий.
VIRUS_POS = {"hbsag": 0.03, "anti_hbc": 0.15, "anti_hcv": 0.04, "hiv": 0.01, "cmv_dna": 0.05}
VIRUS_SPECIAL = {("burkitt", "hiv"): 0.2, ("mzl", "anti_hcv"): 0.08, ("ptcl", "ebv_dna"): 0.6,
                 ("hodgkin", "ebv_eber"): 0.4, ("burkitt", "ebv_eber"): 0.3, ("pcnsl", "hiv"): 0.05}
AUTOIMMUNE = {"cll_sll": 0.35, "mzl": 0.3, "tlgll": 0.5, "waldenstrom": 0.25}
AI_POS = {"dat": 0.12, "ana": 0.15, "rf": 0.2, "cryo": 0.05, "aiha": 0.06, "itp": 0.04}


def _d(x: date) -> str:
    return x.isoformat()


def add_for_patient(db, doc: Principal, pid, nosology: str, mm: dict[str, str], dx: date, provenance: str,
                    rng: random.Random) -> int:
    """Исследования одного пациента; возвращает число записей."""
    records = []

    def rec(kind, code, qual=None, value=None, material=None, days=(0, 30), **extra):
        records.append({"kind": kind, "test_code": code, "qual_result": qual, "value": value,
                        "material": material or inv.CATALOG["kinds"][kind]["default_material"],
                        "test_date": _d(dx + timedelta(days=rng.randint(*days))), "provenance": provenance,
                        **extra})

    # ИФТ — у части пациентов, фенотип типичный для нозологии
    flow = FLOW.get(nosology)
    if flow and rng.random() < flow[2]:
        markers, material, _ = flow
        rec("flow", "flow_panel", "positive", round(rng.uniform(15, 92), 1), material, (-30, 10),
            markers=markers, conclusion="Выявлена клональная популяция с указанным фенотипом.")

    # Генетика — согласованно с полями модуля (вывод для индекса = результат исследования)
    if rng.random() < 0.45:
        linked = []
        for t in inv.tests("genetics"):
            for field, mapping in t.get("links", {}).items():
                have = mm.get(field)
                qual = next((q for q, v in mapping.items() if v == have), None)
                if have and have not in core.MISSING_CODES and qual:
                    linked.append((t["code"], qual))
        for code, qual in rng.sample(linked, min(len(linked), rng.randint(1, 3))):
            unit = (inv.test_def("genetics", code) or {}).get("unit")
            value = round(rng.uniform(20, 85), 1) if unit and qual == "positive" else None
            rec("genetics", code, qual, value, days=(0, 45))
        if rng.random() < 0.25:
            rec("genetics", "karyotype", "positive" if rng.random() < 0.4 else "negative", days=(0, 45),
                qual_detail=rng.choice(["46,XY[20]", "46,XX[20]", "47,XY,+12[14]/46,XY[6]",
                                        "46,XX,del(13)(q14q22)[8]/46,XX[12]", "45,X,-Y[10]/46,XY[10]"]))

    # Вирусный статус — у большинства обследованных перед лечением
    if rng.random() < 0.7:
        for code in ("hbsag", "anti_hbc", "anti_hcv", "hiv"):
            p = VIRUS_SPECIAL.get((nosology, code), VIRUS_POS.get(code, 0.02))
            rec("virology", code, "positive" if rng.random() < p else "negative", days=(-20, 10))
        for (nos, code), p in VIRUS_SPECIAL.items():
            if nos == nosology and code not in ("hiv", "anti_hcv"):
                pos = rng.random() < p
                value = rng.randint(1000, 90000) if pos and code == "ebv_dna" else None
                rec("virology", code, "positive" if pos else "negative", value,
                    "tissue" if code == "ebv_eber" else "blood" if code == "ebv_dna" else None, (-20, 10))

    # Аутоиммунный скрининг — чаще при ХЛЛ, ЛМЗ, T-LGLL, МВ
    if rng.random() < AUTOIMMUNE.get(nosology, 0.12):
        for code in rng.sample(list(AI_POS), rng.randint(1, 3)):
            pos = rng.random() < AI_POS[code]
            value = (rng.choice([160, 320, 640]) if code == "ana" and pos else
                     rng.randint(25, 180) if code == "rf" and pos else None)
            rec("autoimmune", code, "positive" if pos else "negative", value, days=(-10, 60))

    for r in records:
        registry.add_investigation(db, doc, pid, {k: v for k, v in r.items() if v is not None})
    return len(records)


def add_for_all(db, physicians: list[Principal], seed: int = 101) -> int:
    """Исследования для уже внесённых синтетических пациентов (dev-БД, созданная до раздела)."""
    rng = random.Random(seed)
    total = 0
    for doc in physicians:
        for row in registry.list_patients(db, doc):
            if row["branch"] != doc.branch_code:
                continue                       # пациента вносит врач его филиала
            card = registry.get_card(db, doc, row["patient_id"])
            pt = card["patient"]
            total += add_for_patient(db, doc, pt["patient_id"], pt["who_haem5"], card["marker_map"],
                                     pt["dx_date"], pt["provenance"], rng)
    return total

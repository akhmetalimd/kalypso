"""Синтетические пациенты нозологий по описанию (SPECS): MCL, ЛХ, WM, MZL, PTCL.

Генератор общий: возраст, пол, стадия, ECOG, поля модуля и анализы на диагнозе — из
описания нозологии; индекс карточки вносится равным расчёту по тем же данным
(kalypso.indices), чтобы сверка «карточка ↔ расчёт» совпадала; хронология — линии,
ответ по критериям нозологии, рецидивы/прогрессия, смерть. Всё — через доменные сервисы.
Намеренные дефекты, как в остальной синтетике: пропуски полей, коды пропуска, линии
без оценки ответа, часть индексов — кодом пропуска.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import registry_core as core
from kalypso import indices, registry
from kalypso.db import Database
from kalypso.rbac import Principal

REFERENCE_DATE = date(2026, 9, 30)


def _d(x: date) -> str:
    return x.isoformat()


def _months(rng, lo, hi) -> timedelta:
    return timedelta(days=int(rng.uniform(lo, hi) * 30.4))


def pick(rng: random.Random, weighted: dict):
    return rng.choices(list(weighted), weights=list(weighted.values()))[0]


def _risk(result: dict | None, name: str | None) -> float:
    """0 (низкий) … 1 (высокий) — по группе индекса; без индекса — средний риск."""
    if not result or name not in core.INDEXES:
        return 0.5
    groups = [g for _, g in core.INDEXES[name]["groups"]]
    return groups.index(result["group"]) / max(1, len(groups) - 1)


def _response(rng, criteria: str, complete: bool, line_no: int, basis: str | None = None) -> dict:
    if criteria not in ("IWWM-11", "Lugano 2014"):          # IPCG, HCL, ISCL, T-LGLL: CR — полный ответ
        values = core.RESPONSES[criteria]
        value = values[0] if complete else rng.choice([v for v in values[1:]])
        return {"response_value": value, "response_basis": basis or "clinical"}
    if criteria == "IWWM-11":
        value = pick(rng, {"CR": 15, "VGPR": 85}) if complete else pick(rng, {"PR": 55, "MR": 20, "SD": 15, "PD": 10})
        return {"response_value": value, "response_basis": "laboratory"}
    if complete:
        value, ds = "CMR", rng.choice([1, 2, 2, 3])
    else:
        value = pick(rng, {"PMR": 60, "NMR": 15, "PMD": 25})
        ds = 5 if value == "PMD" else rng.choice([4, 4, 5])
    return {"response_value": value, "response_basis": "pet_ct", "deauville": ds,
            "pet_ct_report": f"ПЭТ-КТ после {line_no}-й линии: Deauville {ds}."}


def one_patient(db: Database, doc: Principal, rng: random.Random, nosology: str, iin_for) -> str:
    spec = SPECS[nosology]
    node = core.find_node(core.NOSOLOGY_TREE, nosology)
    mean, sd, lo, hi = spec["age"]
    age = max(lo, min(hi, int(rng.gauss(mean, sd))))
    dx = date(2015, 1, 1) + timedelta(days=rng.randint(0, int(10.5 * 365)))
    sex = "M" if rng.random() < spec["male"] else "F"
    birth = date(dx.year - age, rng.randint(1, 12), rng.randint(1, 28))
    iin = iin_for(rng, birth, sex)
    provenance = "retrospective" if dx < date(2024, 1, 1) else "prospective"
    system = (node.get("staging") or [None])[0]
    stage = pick(rng, spec["stage"]) if system else None
    ctx = {"age": age, "sex": sex, "stage": stage, "adv": stage in ("III", "IV"), "rng": rng}
    ecog = pick(rng, spec["ecog"])
    b_symptoms = pick(rng, spec.get("b_symptoms", {"no": 70, "yes": 25, "unknown": 5}))
    ctx.update(ecog=ecog, b_symptoms=b_symptoms)
    fields = spec["fields"](rng, ctx)
    labs = spec["labs"](rng, ctx, fields)

    # Индекс карточки = расчёт по тем же данным (точный возраст, анализы на диагнозе).
    pseudo = {"who_haem5": nosology, "birth_date": birth, "dx_date": dx, "ecog": ecog, "sex": sex,
              "stage_system": system if stage else None, "stage_value": stage, "b_symptoms": b_symptoms}
    lab_rows = [{"loinc": l, "obs_date": dx, "value_canonical": core.canonicalize_lab(l, v, u).value_canonical}
                for l, v, u in labs]
    mm = {k: v for k, v in fields.items() if v is not None}
    stored = next((ix for ix in indices.computed_indices(pseudo, mm, lab_rows) if ix["stored"]), None)
    risk = _risk(stored and stored["result"], node.get("index"))

    # --- хронология ---
    timeline: list[tuple[str, dict]] = []
    if spec.get("watch_if", lambda f: False)(fields) or rng.random() < spec.get("watch", 0.05) * (1.5 - risk):
        start = dx + _months(rng, 6, 48) if rng.random() < 0.5 else None      # watch & wait
    else:
        start = dx + _months(rng, 0.3, 1.5)
    t, progressing = dx, False
    plan = []
    if start is not None:
        plan = [spec["first"](rng, ctx, fields)] + rng.sample(spec["later"], min(2, len(spec["later"])))
    p_complete = spec["p_complete"](risk)
    for i, (name, drugs, cycles) in enumerate(plan):
        if i:
            start = t + _months(rng, 0.5, 4)
        end = start + _months(rng, *spec.get("duration", (4, 7)))
        if end > REFERENCE_DATE:
            break
        timeline.append(("line", {"regimen": name, "drug": drugs, "cycles": cycles, "start_date": _d(start),
                                  "end_date": _d(end), "stop_reason": "завершён план"}))
        pet2 = start + _months(rng, 1.8, 2.5)
        if i == 0 and spec.get("interim_pet") and pet2 < end and rng.random() < 0.8:   # ПЭТ-2 во время линии
            ds = rng.choice([1, 2, 3, 3]) if rng.random() < p_complete else rng.choice([4, 4, 5])
            timeline.append(("response", {"obs_date": _d(pet2), "line_no": 1, "deauville": ds,
                                          "pet_ct_report": f"Промежуточная ПЭТ-КТ (ПЭТ-2): Deauville {ds}."}))
        r_date = end + _months(rng, 0.7, 2)
        if r_date > REFERENCE_DATE:
            break
        complete = rng.random() < (p_complete if i == 0 else p_complete * 0.6)
        if rng.random() < 0.88:                          # у части линий оценки ответа нет — флаг полноты
            resp = _response(rng, node["response_criteria"], complete, i + 1, spec.get("response_basis"))
            timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1, **resp}))
        if complete and rng.random() >= spec["p_relapse"](risk):
            progressing = False
            break                                        # стойкая ремиссия
        if not complete and rng.random() >= spec.get("partial_progress", 1.0):
            progressing = False
            break                                        # частичный ответ без прогрессии за наблюдение
        lo_m, hi_m = spec["relapse_months"] if complete else spec.get("partial_months", (1, 8))
        event_date = r_date + _months(rng, lo_m, hi_m)
        if event_date > REFERENCE_DATE:
            break
        timeline.append(("event", {"obs_date": _d(event_date), "event_type": "relapse" if complete else "progression"}))
        t, progressing = event_date, True

    last_event = max([dx] + [date.fromisoformat(e.get("obs_date") or e.get("end_date") or e["start_date"])
                             for _, e in timeline])
    death = cause = None
    if progressing and rng.random() < spec.get("death_progressive", 0.6):
        death, cause = last_event + _months(rng, 1, 18), pick(rng, {"lpd_related": 85, "unknown": 15})
    elif rng.random() < 0.05:
        death, cause = last_event + _months(rng, 6, 60), pick(rng, {"not_related": 75, "unknown": 25})
    if death and death > REFERENCE_DATE:
        death = None
    lost = death is None and rng.random() < 0.04
    last_contact = death or min(REFERENCE_DATE, last_event + _months(rng, 2, 30 if lost else 22))

    # --- запись через сервисы ---
    data = {
        "iin": iin, "sex": sex, "birth_date": _d(birth), "who_haem5": nosology,
        "dx_date": _d(dx), "last_contact_date": _d(last_contact), "provenance": provenance,
        "status_last_contact": "lost_to_follow_up" if lost else "alive",
        "ecog": ecog, "b_symptoms": b_symptoms,
        "first_visit_date": _d(dx - timedelta(days=rng.randint(0, 45))),
        "histology_report": spec["histology"](rng, fields) if rng.random() < 0.75 else None,
    }
    if stage:
        data.update(stage_system=system, stage_value=stage)
    if age >= 65 and rng.random() < 0.7:
        data["cirs_score"] = rng.randint(0, 14)
    else:
        data["cirs_score__missing"] = "not_done"
    if stored and stored["result"] and rng.random() < 0.85:
        data["index_score"] = stored["result"]["score"]
        data["index_group"] = stored["result"]["group"]
    elif node.get("index") in core.INDEXES:
        data["index_score__missing"] = "unknown"
    pid = registry.register_patient(db, doc, data).record_id

    for key, val in fields.items():
        if val is None:
            continue
        rec = {"field_key": key, "provenance": provenance, "test_date": _d(dx)}
        rec.update({"missing": val} if val in core.MISSING_CODES else {"value": val})
        registry.add_marker(db, doc, pid, rec)
    for loinc, value, unit in labs:
        registry.add_lab(db, doc, pid, {"loinc": loinc, "value": value, "unit": unit,
                                        "obs_date": _d(dx), "provenance": provenance})
    line_ids: dict[int, str] = {}
    for kind, e in timeline:
        if kind == "line":
            line_ids[len(line_ids) + 1] = str(registry.add_line(db, doc, pid, {**e, "provenance": provenance}).record_id)
        elif kind == "response":
            e = dict(e)
            line_id = line_ids.get(e.pop("line_no"))
            registry.add_observation(db, doc, pid, {**e, "line_id": line_id, "provenance": provenance})
        else:
            registry.add_observation(db, doc, pid, {**e, "provenance": provenance})
    if death:
        registry.add_observation(db, doc, pid, {"obs_date": _d(death), "event_type": "death",
                                                "death_cause": cause, "provenance": provenance})
    return iin


# ---------------------------------------------------------------------------
# Описания нозологий
# ---------------------------------------------------------------------------
def _maybe(rng, value, p_missing=0.05, code="not_done"):
    r = rng.random()
    return None if r < p_missing / 2 else code if r < p_missing else value


LDH_U_L = {"lt_067": (100, 160), "067_099": (170, 245), "100_149": (255, 370), "ge_150": (380, 900)}  # ВГН 250


def _mcl_fields(rng, ctx):
    return {
        "ldh_ratio": _maybe(rng, pick(rng, {"lt_067": 20, "067_099": 40, "100_149": 25, "ge_150": 15})),
        "ki67": _maybe(rng, "ge_30" if rng.random() < 0.3 else "lt_30", 0.1),
        "tp53_mcl": _maybe(rng, "detected" if rng.random() < 0.15 else "not_detected", 0.2),
        "blastoid": "yes" if rng.random() < 0.12 else "no",
        "leukemic_nn": "yes" if rng.random() < 0.08 else None,
    }


def _mcl_labs(rng, ctx, fields):
    lo, hi = LDH_U_L.get(fields["ldh_ratio"], (150, 300))
    wbc = round(rng.choice([rng.uniform(4, 6.6), rng.uniform(6.7, 9.9), rng.uniform(10, 14.9), rng.uniform(15, 60)]), 1)
    return [("6690-2", wbc, "10*9/L"), ("2532-0", rng.randint(lo, hi), "U/L"),
            ("8302-2", round(rng.uniform(95, 150)), "g/L"), ("777-3", rng.randint(90, 350), "10*9/L")]


def _mcl_first(rng, ctx, fields):
    if ctx["age"] < 65:
        return pick(rng, {("R-CHOP / R-DHAP + аутоТГСК", "R-CHOP и R-DHAP, ВДХТ, аутоТГСК", 6): 60,
                          ("R-CHOP / R-DHAP + ибрутиниб", "R-CHOP и R-DHAP, ибрутиниб", 6): 25,
                          ("BR", "бендамустин, ритуксимаб", 6): 15})
    return pick(rng, {("BR", "бендамустин, ритуксимаб", 6): 45, ("VR-CAP", "бортезомиб, ритуксимаб, циклофосфамид, доксорубицин, преднизолон", 6): 15,
                      ("R-BAC", "ритуксимаб, бендамустин, цитарабин", 4): 15,
                      ("Акалабрутиниб + BR", "акалабрутиниб, бендамустин, ритуксимаб", 6): 15,
                      ("Ибрутиниб + ритуксимаб", "ибрутиниб, ритуксимаб", None): 10})


def _hl_fields(rng, ctx):
    adv = ctx["adv"]
    return {
        "hl_subtype": _maybe(rng, pick(rng, {"ns": 70, "mc": 20, "lr": 6, "ld": 4}), 0.06),
        "bulky_med": "yes" if rng.random() < (0.35 if adv else 0.25) else "no",
        "esr_raised": _maybe(rng, "yes" if rng.random() < (0.55 if adv else 0.35) else "no", 0.08),
        "nodal_areas_hl": "ge_3" if rng.random() < (0.7 if adv else 0.4) else "lt_3",
        "extranodal_hl": "yes" if ctx["stage"] in ("IE", "IIE") or (adv and rng.random() < 0.3) else "no",
    }


def _hl_labs(rng, ctx, fields):
    adv = ctx["adv"]
    wbc = round(rng.uniform(15, 22) if rng.random() < (0.2 if adv else 0.05) else rng.uniform(4.5, 14), 1)
    lymph = round(rng.uniform(0.3, 0.59) if rng.random() < (0.25 if adv else 0.08) else rng.uniform(0.8, 3.0), 2)
    return [("1751-7", round(rng.uniform(30, 39.5) if rng.random() < (0.4 if adv else 0.1) else rng.uniform(40, 48), 1), "g/L"),
            ("8302-2", round(rng.uniform(90, 104) if rng.random() < (0.25 if adv else 0.05) else rng.uniform(106, 155)), "g/L"),
            ("6690-2", wbc, "10*9/L"), ("731-0", lymph, "10*9/L")]


def _hl_first(rng, ctx, fields):
    if ctx["adv"]:
        return pick(rng, {("BrECADD", "брентуксимаб, этопозид, циклофосфамид, доксорубицин, дакарбазин, дексаметазон", 6): 30,
                          ("BEACOPP-esc", "блеомицин, этопозид, доксорубицин, циклофосфамид, винкристин, прокарбазин, преднизолон", 6): 25,
                          ("ABVD", "доксорубицин, блеомицин, винбластин, дакарбазин", 6): 25,
                          ("A-AVD", "брентуксимаб, доксорубицин, винбластин, дакарбазин", 6): 20})
    unfav = any(fields.get(k) == "yes" for k in ("bulky_med", "esr_raised", "extranodal_hl")) or fields["nodal_areas_hl"] == "ge_3"
    if unfav:
        return pick(rng, {("ABVD + ЛТ", "ABVD ×4, ЛТ 30 Гр", 4): 60, ("BEACOPP-esc ×2 + ABVD ×2", "BEACOPP-esc, ABVD, ЛТ", 4): 40})
    return ("ABVD + ЛТ", "ABVD ×2, ЛТ 20 Гр", 2)


def _mzl_fields(rng, ctx):
    subtype = pick(rng, {"malt": 60, "nodal": 20, "splenic": 20})
    site = pick(rng, {"stomach": 40, "ocular_adnexa": 15, "lung": 10, "salivary": 15, "skin": 10, "other": 10}) \
        if subtype == "malt" else None
    return {
        "mzl_subtype": subtype,
        "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 80, "uln_1_3": 18, "gt_3_uln": 2})),
        "hcv": _maybe(rng, "detected" if rng.random() < (0.12 if subtype == "splenic" else 0.04) else "not_detected", 0.1),
        "malt_site": site,
        "hp": ("detected" if rng.random() < 0.7 else "not_detected") if site == "stomach" else None,
    }


def _mzl_labs(rng, fields):
    lo, hi = {"le_uln": (140, 245), "uln_1_3": (260, 700), "gt_3_uln": (760, 1100)}.get(fields["ldh_uln"], (150, 300))
    return [("2532-0", rng.randint(lo, hi), "U/L"), ("8302-2", round(rng.uniform(100, 155)), "g/L"),
            ("777-3", rng.randint(90, 350), "10*9/L")]


def _mzl_first(rng, ctx, fields):
    if fields["hp"] == "detected":
        return ("Эрадикация H. pylori", "ингибитор протонной помпы, кларитромицин, амоксициллин", None)
    if fields["mzl_subtype"] == "malt" and not ctx["adv"]:
        return ("Лучевая терапия", "ЛТ 24 Гр на зону поражения", None)
    if fields["mzl_subtype"] == "splenic":
        return pick(rng, {("Ритуксимаб (монотерапия)", "ритуксимаб", 6): 50, ("Спленэктомия", "спленэктомия", None): 20,
                          ("BR", "бендамустин, ритуксимаб", 6): 30})
    return pick(rng, {("BR", "бендамустин, ритуксимаб", 6): 40, ("Ритуксимаб (монотерапия)", "ритуксимаб", 4): 25,
                      ("Ритуксимаб + хлорамбуцил", "ритуксимаб, хлорамбуцил", 6): 20,
                      ("R-CHOP", "ритуксимаб, циклофосфамид, доксорубицин, винкристин, преднизолон", 6): 15})


def _ptcl_fields(rng, ctx):
    subtype = pick(rng, {"ptcl_nos": 35, "aitl": 25, "alcl_alk_neg": 15, "alcl_alk_pos": 10, "enktl": 10, "other": 5})
    enktl = subtype == "enktl"
    return {
        "ptcl_subtype": subtype,
        "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 40, "uln_1_3": 45, "gt_3_uln": 15} if ctx["adv"] else
                                    {"le_uln": 65, "uln_1_3": 30, "gt_3_uln": 5})),
        "bm_involvement": _maybe(rng, "yes" if ctx["stage"] == "IV" and rng.random() < 0.35 else "no", 0.1),
        "extranodal_n": pick(rng, {"0": 10, "1": 40, "2plus": 50} if ctx["stage"] == "IV" else {"0": 70, "1": 30}),
        "nasal_type": ("yes" if rng.random() < 0.75 else "no") if enktl else None,
        "distant_ln": ("yes" if ctx["adv"] and rng.random() < 0.5 else "no") if enktl else None,
        "ebv_dna": ("detected" if rng.random() < 0.6 else "not_detected") if enktl else None,
    }


def _ptcl_first(rng, ctx, f):
    if f["ptcl_subtype"] == "enktl":
        return pick(rng, {("P-GemOx + ЛТ", "пэгаспаргаза, гемцитабин, оксалиплатин, ЛТ", 4): 45,
                          ("DDGP", "дексаметазон, цисплатин, гемцитабин, пэгаспаргаза", 6): 30,
                          ("SMILE", "дексаметазон, метотрексат, ифосфамид, L-аспарагиназа, этопозид", 4): 25})
    if f["ptcl_subtype"] in ("alcl_alk_pos", "alcl_alk_neg"):
        return pick(rng, {("BV-CHP (брентуксимаб + CHP)", "брентуксимаб ведотин, циклофосфамид, доксорубицин, преднизолон", 6): 60,
                          ("CHOEP", "CHOP + этопозид", 6): 25, ("CHOP", "циклофосфамид, доксорубицин, винкристин, преднизолон", 6): 15})
    if ctx["age"] < 60:
        return ("CHOEP", "CHOP + этопозид", 6)
    return ("CHOP", "циклофосфамид, доксорубицин, винкристин, преднизолон", 6)


def _hgbl_fields(rng, ctx):
    subtype = "myc_bcl2" if rng.random() < 0.7 else "hgbl_nos"
    if subtype == "myc_bcl2":
        myc, bcl2 = "detected", "detected"
    else:
        myc, bcl2 = pick(rng, {"detected": 30, "not_detected": 70}), "not_detected"
    stage4 = ctx["stage"] == "IV"
    return {
        "hgbl_subtype": subtype, "myc_r": myc, "bcl2_r": bcl2,
        "bcl6_r": _maybe(rng, "detected" if rng.random() < 0.25 else "not_detected", 0.15),
        "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 20, "uln_1_3": 50, "gt_3_uln": 30})),
        "extranodal_n": pick(rng, {"0": 10, "1": 35, "2plus": 55} if stage4 else {"0": 60, "1": 40}),
        "kidney_adrenal": "yes" if stage4 and rng.random() < 0.2 else "no",
        "cns_involvement": "yes" if rng.random() < 0.08 else "no",
    }

_MF_TNMB = {   # стадия -> варианты T, N, M, B (согласованные с ISCL/EORTC)
    "IA": (("T1a", "T1b"), ("N0",), ("M0",), ("B0",)), "IB": (("T2a", "T2b"), ("N0",), ("M0",), ("B0",)),
    "IIA": (("T1b", "T2b"), ("N1", "N2"), ("M0",), ("B0",)), "IIB": (("T3",), ("N0", "N1"), ("M0",), ("B0",)),
    "IIIA": (("T4",), ("N0",), ("M0",), ("B0",)), "IIIB": (("T4",), ("N0", "N1"), ("M0",), ("B1",)),
    "IVA1": (("T4", "T3"), ("N0", "N1"), ("M0",), ("B2",)), "IVA2": (("T3", "T4"), ("N3",), ("M0",), ("B0", "B1")),
    "IVB": (("T3", "T4"), ("N1", "N2"), ("M1",), ("B0", "B2")),
}


def _mf_fields(rng, ctx):
    t, n, m, b = (rng.choice(opts) for opts in _MF_TNMB[ctx["stage"]])
    return {"t_cat": t, "n_cat": n, "m_cat": m, "b_cat": b,
            "lct": "yes" if rng.random() < (0.25 if ctx["stage"] >= "IIB" else 0.03) else "no",
            "folliculotropic": "yes" if rng.random() < 0.15 else "no"}


def _mf_first(rng, ctx, f):
    if ctx["stage"] in ("IA", "IB", "IIA"):
        return pick(rng, {("Топические кортикостероиды", "клобетазол", None): 35, ("ПУВА-терапия", "ПУВА", None): 35,
                          ("UVB-311 нм", "узкополосная UVB", None): 30})
    if f["b_cat"] == "B2":
        return pick(rng, {("Экстракорпоральный фотоферез", "ЭКФ", None): 40, ("Могамулизумаб", "могамулизумаб", None): 40,
                          ("Бексаротен", "бексаротен", None): 20})
    return pick(rng, {("Бексаротен", "бексаротен", None): 25, ("Интерферон-α", "интерферон-α", None): 20,
                      ("Брентуксимаб ведотин", "брентуксимаб ведотин", 8): 20, ("Могамулизумаб", "могамулизумаб", None): 15,
                      ("Тотальное облучение кожи электронами (TSEBT)", "TSEBT", None): 20})

SPECS = {
    "mcl": {
        "age": (67, 9, 38, 90), "male": 0.75, "watch": 0.08,
        "stage": {"I": 3, "IE": 2, "II": 7, "IIE": 2, "III": 16, "IV": 70},
        "ecog": {0: 35, 1: 45, 2: 15, 3: 5},
        "fields": _mcl_fields, "labs": _mcl_labs, "first": _mcl_first,
        "later": [("Ибрутиниб", "ибрутиниб", None), ("Занубрутиниб", "занубрутиниб", None),
                  ("Пиртобрутиниб", "пиртобрутиниб", None), ("CAR-T (brexu-cel)", "брексукабтаген аутолейсел", 1),
                  ("Венетоклакс + ибрутиниб", "венетоклакс, ибрутиниб", None)],
        "p_complete": lambda risk: 0.8 - 0.35 * risk, "p_relapse": lambda risk: 0.55 + 0.3 * risk,
        "relapse_months": (8, 60), "death_progressive": 0.55,
        "histology": lambda rng, f: ("Биопсия лимфоузла: диффузная/нодулярная пролиферация мелких и средних клеток "
                                     f"с неправильными ядрами. ИГХ: CD20+, CD5+, cyclin D1+, SOX11+, CD23-. Ki-67 "
                                     f"{rng.randint(31, 70) if f.get('ki67') == 'ge_30' else rng.randint(5, 29)}%. "
                                     "Заключение: мантийноклеточная лимфома."),
    },
    "hodgkin": {
        "age": (34, 15, 18, 80), "male": 0.55, "watch": 0.0, "interim_pet": True,
        "stage": {"I": 6, "IE": 2, "II": 36, "IIE": 6, "III": 22, "IV": 28},
        "ecog": {0: 60, 1: 30, 2: 8, 3: 2},
        "b_symptoms": {"no": 60, "yes": 37, "unknown": 3},
        "fields": _hl_fields, "labs": _hl_labs, "first": _hl_first,
        "later": [("ICE + аутоТГСК", "ифосфамид, карбоплатин, этопозид, ВДХТ, аутоТГСК", 2),
                  ("DHAP + аутоТГСК", "дексаметазон, цитарабин, цисплатин, ВДХТ, аутоТГСК", 2),
                  ("Брентуксимаб ведотин", "брентуксимаб ведотин", 16), ("Ниволумаб", "ниволумаб", 12),
                  ("Пембролизумаб", "пембролизумаб", 12)],
        "p_complete": lambda risk: 0.92 - 0.25 * risk, "p_relapse": lambda risk: 0.08 + 0.2 * risk,
        "relapse_months": (6, 48), "death_progressive": 0.3,
        "histology": lambda rng, f: ("Биопсия лимфоузла: клетки Ходжкина и Рид–Штернберга на фоне реактивного "
                                     "микроокружения. ИГХ: CD30+, CD15+, PAX5 слабо+, CD20-, CD45-. "
                                     "Заключение: классическая лимфома Ходжкина."),
    },
    "waldenstrom": {
        "age": (68, 9, 40, 90), "male": 0.6, "watch_if": lambda f: f["treat_indication"] == "no",
        "stage": {}, "ecog": {0: 40, 1: 40, 2: 15, 3: 5},
        "fields": lambda rng, ctx: {
            "myd88": _maybe(rng, "detected" if rng.random() < 0.9 else "not_detected", 0.1),
            "cxcr4": _maybe(rng, "detected" if rng.random() < 0.35 else "not_detected", 0.25),
            "treat_indication": "yes" if rng.random() < 0.65 else "no",
            "hyperviscosity": "yes" if rng.random() < 0.1 else None,
        },
        "labs": lambda rng, ctx, f: [
            ("8302-2", round(rng.uniform(85, 115) if rng.random() < 0.5 else rng.uniform(116, 150)), "g/L"),
            ("777-3", rng.randint(50, 100) if rng.random() < 0.15 else rng.randint(101, 350), "10*9/L"),
            ("1952-1", round(rng.uniform(3.1, 7.5) if rng.random() < 0.5 else rng.uniform(1.5, 3.0), 1), "mg/L"),
            ("2472-0", round(rng.uniform(71, 95) if rng.random() < 0.15 else rng.uniform(5, 70), 1), "g/L"),
            ("2532-0", rng.randint(250, 380) if rng.random() < 0.2 else rng.randint(140, 249), "U/L"),
            ("1751-7", round(rng.uniform(28, 34.9) if rng.random() < 0.25 else rng.uniform(35, 46), 1), "g/L"),
        ],
        "first": lambda rng, ctx, f: pick(rng, {
            ("DRC (дексаметазон, ритуксимаб, циклофосфамид)", "дексаметазон, ритуксимаб, циклофосфамид", 6): 35,
            ("BR", "бендамустин, ритуксимаб", 6): 35, ("Занубрутиниб", "занубрутиниб", None): 15,
            ("Ибрутиниб + ритуксимаб", "ибрутиниб, ритуксимаб", None): 10,
            ("Ритуксимаб (монотерапия)", "ритуксимаб", 4): 5}),
        "later": [("Занубрутиниб", "занубрутиниб", None), ("BR", "бендамустин, ритуксимаб", 6),
                  ("BDR (бортезомиб, дексаметазон, ритуксимаб)", "бортезомиб, дексаметазон, ритуксимаб", 6),
                  ("Пиртобрутиниб", "пиртобрутиниб", None)],
        "p_complete": lambda risk: 0.35 - 0.15 * risk, "p_relapse": lambda risk: 0.35 + 0.3 * risk,
        "relapse_months": (24, 84), "partial_progress": 0.6, "partial_months": (12, 60), "death_progressive": 0.3,
        "histology": lambda rng, f: ("Трепанобиопсия: интерстициальная инфильтрация лимфоплазмоцитарными клетками "
                                     f"({rng.randint(20, 70)} %). ИГХ: CD20+, CD138+ (плазмоцитарный компонент), IgM κ. "
                                     "Заключение: лимфоплазмоцитарная лимфома / макроглобулинемия Вальденстрёма."),
    },
    "mzl": {
        "age": (66, 11, 30, 90), "male": 0.45, "watch": 0.2,
        "stage": {"I": 18, "IE": 27, "II": 8, "IIE": 10, "III": 10, "IV": 27},
        "ecog": {0: 55, 1: 35, 2: 8, 3: 2},
        "fields": lambda rng, ctx: _mzl_fields(rng, ctx), "labs": lambda rng, ctx, f: _mzl_labs(rng, f),
        "first": lambda rng, ctx, f: _mzl_first(rng, ctx, f),
        "later": [("BR", "бендамустин, ритуксимаб", 6), ("Ибрутиниб", "ибрутиниб", None),
                  ("Занубрутиниб", "занубрутиниб", None), ("Леналидомид + ритуксимаб (R²)", "леналидомид, ритуксимаб", 12)],
        "p_complete": lambda risk: 0.75 - 0.3 * risk, "p_relapse": lambda risk: 0.3 + 0.3 * risk,
        "relapse_months": (24, 96), "partial_progress": 0.6, "partial_months": (12, 60), "death_progressive": 0.3,
        "histology": lambda rng, f: ("Биопсия: инфильтрация мелкими клетками маргинальной зоны (моноцитоидные B-клетки), "
                                     "лимфоэпителиальные поражения. ИГХ: CD20+, CD5-, CD10-, cyclin D1-, CD23-. "
                                     "Заключение: лимфома маргинальной зоны."),
    },
    "ptcl": {
        "age": (60, 14, 20, 88), "male": 0.62, "watch": 0.0,
        "stage": {"I": 6, "IE": 8, "II": 8, "IIE": 6, "III": 22, "IV": 50},
        "ecog": {0: 25, 1: 40, 2: 22, 3: 10, 4: 3},
        "fields": _ptcl_fields, "first": _ptcl_first,
        "labs": lambda rng, ctx, f: [("2532-0", rng.randint(*{"le_uln": (140, 245), "uln_1_3": (260, 740),
                                                              "gt_3_uln": (760, 1500)}.get(f["ldh_uln"], (150, 400))), "U/L"),
                                     ("8302-2", round(rng.uniform(90, 150)), "g/L")],
        "later": [("ВДХТ + аутоТГСК", "ВДХТ (BEAM), аутоТГСК", 1), ("GDP", "гемцитабин, дексаметазон, цисплатин", 4),
                  ("Брентуксимаб ведотин", "брентуксимаб ведотин", 8), ("Ромидепсин", "ромидепсин", None),
                  ("Пралатрексат", "пралатрексат", None)],
        "p_complete": lambda risk: 0.65 - 0.35 * risk, "p_relapse": lambda risk: 0.45 + 0.35 * risk,
        "relapse_months": (4, 36), "death_progressive": 0.7,
        "histology": lambda rng, f: ("Биопсия: диффузная инфильтрация атипичными T-клетками среднего и крупного размера. "
                                     f"ИГХ: CD3+, CD4+/CD8-, потеря CD7, CD30 {'+' if 'alcl' in f['ptcl_subtype'] else '-'}. "
                                     "Ki-67 60–80 %. Заключение: периферическая T-клеточная лимфома."),
    },
    "hgbl": {
        "age": (62, 12, 25, 88), "male": 0.6, "watch": 0.0,
        "stage": {"I": 4, "IE": 3, "II": 8, "IIE": 5, "III": 20, "IV": 60},
        "ecog": {0: 20, 1: 40, 2: 25, 3: 12, 4: 3},
        "fields": _hgbl_fields,
        "labs": lambda rng, ctx, f: [("2532-0", rng.randint(*{"le_uln": (140, 245), "uln_1_3": (260, 740),
                                                              "gt_3_uln": (760, 2000)}.get(f["ldh_uln"], (200, 600))), "U/L"),
                                     ("8302-2", round(rng.uniform(85, 145)), "g/L")],
        "first": lambda rng, ctx, f: pick(rng, {("DA-EPOCH-R", "дозоадаптированный EPOCH, ритуксимаб", 6): 55,
                                                 ("R-CHOP", "ритуксимаб, циклофосфамид, доксорубицин, винкристин, преднизолон", 6): 30,
                                                 ("R-hyper-CVAD / R-MA", "hyper-CVAD, метотрексат, цитарабин, ритуксимаб", 8): 15}),
        "later": [("R-ICE", "ритуксимаб, ифосфамид, карбоплатин, этопозид", 3), ("CAR-T (axi-cel)", "аксикабтаген цилолейсел", 1),
                  ("Глофитамаб", "глофитамаб", 12), ("Эпкоритамаб", "эпкоритамаб", None)],
        "p_complete": lambda risk: 0.65 - 0.35 * risk, "p_relapse": lambda risk: 0.3 + 0.3 * risk,
        "relapse_months": (3, 24), "death_progressive": 0.75,
        "histology": lambda rng, f: ("Биопсия: диффузный рост крупных/промежуточных клеток, высокий митотический индекс. "
                                     "ИГХ: CD20+, CD10+, BCL2+, MYC+ (> 40 %). Ki-67 90 %. "
                                     "Заключение: B-клеточная лимфома высокой степени злокачественности."),
    },
    "pmbcl": {
        "age": (35, 10, 18, 70), "male": 0.4, "watch": 0.0,
        "stage": {"I": 10, "IE": 5, "II": 45, "IIE": 15, "III": 5, "IV": 20},
        "ecog": {0: 45, 1: 35, 2: 15, 3: 5},
        "fields": lambda rng, ctx: {
            "bulky_med": "yes" if rng.random() < 0.7 else "no",
            "effusion": _maybe(rng, "yes" if rng.random() < 0.35 else "no"),
            "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 25, "uln_1_3": 55, "gt_3_uln": 20})),
            "svc_syndrome": "yes" if rng.random() < 0.25 else None,
        },
        "labs": lambda rng, ctx, f: [("2532-0", rng.randint(*{"le_uln": (140, 245), "uln_1_3": (260, 740),
                                                              "gt_3_uln": (760, 1400)}.get(f["ldh_uln"], (200, 500))), "U/L")],
        "first": lambda rng, ctx, f: pick(rng, {("DA-EPOCH-R", "дозоадаптированный EPOCH, ритуксимаб", 6): 60,
                                                 ("R-CHOP + ЛТ", "R-CHOP ×6, ЛТ на средостение 30 Гр", 6): 40}),
        "later": [("Пембролизумаб", "пембролизумаб", 12), ("R-ICE + аутоТГСК", "R-ICE, ВДХТ, аутоТГСК", 3),
                  ("Ниволумаб + брентуксимаб", "ниволумаб, брентуксимаб ведотин", 8), ("CAR-T (axi-cel)", "аксикабтаген цилолейсел", 1)],
        "p_complete": lambda risk: 0.88 - 0.3 * risk, "p_relapse": lambda risk: 0.1 + 0.2 * risk,
        "relapse_months": (3, 24), "death_progressive": 0.55,
        "histology": lambda rng, f: ("Биопсия опухоли средостения: крупные клетки со светлой цитоплазмой, склероз. "
                                     "ИГХ: CD20+, CD23+, CD30 слабо+, PD-L1+, MAL+. "
                                     "Заключение: первичная медиастинальная B-клеточная лимфома."),
    },
    "burkitt": {
        "age": (33, 16, 18, 80), "male": 0.75, "watch": 0.0,
        "stage": {"I": 8, "IE": 7, "II": 10, "IIE": 10, "III": 10, "IV": 55},
        "ecog": {0: 25, 1: 35, 2: 25, 3: 12, 4: 3},
        "fields": lambda rng, ctx: {
            "myc_ig": _maybe(rng, "detected" if rng.random() < 0.95 else "not_detected", 0.06),
            "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 15, "uln_1_3": 45, "gt_3_uln": 40})),
            "cns_involvement": "yes" if rng.random() < 0.12 else "no",
            "bm_involvement": "yes" if ctx["stage"] == "IV" and rng.random() < 0.45 else "no",
            "hiv": _maybe(rng, "detected" if rng.random() < 0.2 else "not_detected", 0.08),
            "ebv": "detected" if rng.random() < 0.3 else None,
        },
        "labs": lambda rng, ctx, f: [("2532-0", rng.randint(*{"le_uln": (140, 245), "uln_1_3": (260, 740),
                                                              "gt_3_uln": (760, 3000)}.get(f["ldh_uln"], (300, 900))), "U/L"),
                                     ("3084-1", rng.randint(250, 600), "umol/L")],
        "first": lambda rng, ctx, f: (("DA-EPOCH-R", "дозоадаптированный EPOCH, ритуксимаб", 6) if ctx["age"] >= 60 else pick(rng, {
            ("R-CODOX-M / R-IVAC", "R-CODOX-M, R-IVAC", 4): 45, ("DA-EPOCH-R", "дозоадаптированный EPOCH, ритуксимаб", 6): 30,
            ("R-hyper-CVAD / R-MA", "hyper-CVAD, метотрексат, цитарабин, ритуксимаб", 8): 15,
            ("GMALL-B-ALL/NHL", "протокол GMALL", 6): 10})),
        "duration": (3, 5),
        "later": [("R-ICE", "ритуксимаб, ифосфамид, карбоплатин, этопозид", 3), ("ВДХТ + аутоТГСК", "ВДХТ (BEAM), аутоТГСК", 1),
                  ("CAR-T", "CAR-T (в исследовании)", 1)],
        "p_complete": lambda risk: 0.88 - 0.4 * risk, "p_relapse": lambda risk: 0.08 + 0.2 * risk,
        "relapse_months": (2, 12), "death_progressive": 0.85,
        "histology": lambda rng, f: ("Биопсия: монотонная пролиферация средних клеток, «звёздное небо». ИГХ: CD20+, CD10+, "
                                     "BCL6+, BCL2-, TdT-. Ki-67 > 95 %. Заключение: лимфома Бёркитта."),
    },
    "pcnsl": {
        "age": (63, 10, 25, 88), "male": 0.55, "watch": 0.0, "response_basis": "mri",
        "stage": {}, "ecog": {0: 20, 1: 35, 2: 25, 3: 15, 4: 5},
        "fields": lambda rng, ctx: {
            "ldh_uln": _maybe(rng, pick(rng, {"le_uln": 60, "uln_1_3": 35, "gt_3_uln": 5})),
            "csf_protein": _maybe(rng, "yes" if rng.random() < 0.5 else "no", 0.2),
            "deep_brain": "yes" if rng.random() < 0.55 else "no",
            "kps": "ge_70" if ctx["ecog"] <= 1 else "lt_70",
            "ocular": "yes" if rng.random() < 0.15 else None,
            "csf_cytology": ("detected" if rng.random() < 0.15 else "not_detected") if rng.random() < 0.6 else None,
        },
        "labs": lambda rng, ctx, f: [("2532-0", rng.randint(*{"le_uln": (140, 245), "uln_1_3": (260, 600),
                                                              "gt_3_uln": (760, 1000)}.get(f["ldh_uln"], (150, 400))), "U/L"),
                                     ("2160-0", rng.randint(55, 110), "umol/L")],
        "first": lambda rng, ctx, f: (("HD-MTX + ритуксимаб", "метотрексат 3,5 г/м², ритуксимаб", 6) if ctx["age"] >= 70 else pick(rng, {
            ("MATRix", "метотрексат, цитарабин, тиотепа, ритуксимаб", 4): 55, ("R-MPV", "ритуксимаб, метотрексат, прокарбазин, винкристин", 5): 35,
            ("HD-MTX + темозоломид", "метотрексат, темозоломид", 6): 10})),
        "later": [("Тиотепа-ВДХТ + аутоТГСК", "тиотепа, кармустин / бусульфан, аутоТГСК", 1), ("Ибрутиниб", "ибрутиниб", None),
                  ("Облучение всего головного мозга (WBRT)", "WBRT 23,4–36 Гр", None),
                  ("Леналидомид + ритуксимаб (R²)", "леналидомид, ритуксимаб", 12)],
        "p_complete": lambda risk: 0.65 - 0.3 * risk, "p_relapse": lambda risk: 0.4 + 0.3 * risk,
        "relapse_months": (6, 36), "death_progressive": 0.7,
        "histology": lambda rng, f: ("Стереотаксическая биопсия: периваскулярная инфильтрация крупными атипичными B-клетками. "
                                     "ИГХ: CD20+, MUM1+, BCL6+, CD10-. Ki-67 80–90 %. Заключение: первичная ДВКЛ ЦНС."),
    },
    "nlpbcl": {
        "age": (40, 15, 18, 80), "male": 0.75, "watch": 0.1,
        "stage": {"I": 40, "IE": 3, "II": 30, "IIE": 2, "III": 15, "IV": 10},
        "ecog": {0: 70, 1: 25, 2: 5},
        "b_symptoms": {"no": 85, "yes": 12, "unknown": 3},
        "fields": lambda rng, ctx: {
            "nlp_pattern": _maybe(rng, "variant" if rng.random() < 0.3 else "typical", 0.1),
            "bulky_med": "yes" if rng.random() < 0.05 else "no",
            "esr_raised": _maybe(rng, "yes" if rng.random() < 0.2 else "no", 0.1),
            "nodal_areas_hl": "ge_3" if rng.random() < (0.6 if ctx["adv"] else 0.25) else "lt_3",
            "extranodal_hl": "yes" if ctx["stage"] in ("IE", "IIE") else "no",
            "transformation": "yes" if rng.random() < 0.05 else None,
        },
        "labs": lambda rng, ctx, f: [("8302-2", round(rng.uniform(115, 160)), "g/L"), ("2532-0", rng.randint(140, 260), "U/L")],
        "first": lambda rng, ctx, f: pick(rng, {("R-CHOP", "ритуксимаб, CHOP", 6): 45, ("R-ABVD", "ритуксимаб, ABVD", 6): 30,
                                                 ("Ритуксимаб (монотерапия)", "ритуксимаб", 4): 25}) if ctx["adv"]
        else pick(rng, {("Лучевая терапия", "ЛТ 30 Гр на зону поражения", None): 70,
                        ("Ритуксимаб (монотерапия)", "ритуксимаб", 4): 30}),
        "later": [("R-CHOP", "ритуксимаб, CHOP", 6), ("BR", "бендамустин, ритуксимаб", 6), ("R-CVP", "ритуксимаб, CVP", 6)],
        "p_complete": lambda risk: 0.9 - 0.2 * risk, "p_relapse": lambda risk: 0.2 + 0.2 * risk,
        "relapse_months": (12, 96), "partial_progress": 0.5, "partial_months": (12, 48), "death_progressive": 0.2,
        "histology": lambda rng, f: ("Биопсия лимфоузла: нодулярная пролиферация, LP-клетки («попкорн») на фоне малых "
                                     "B-лимфоцитов. ИГХ: CD20+, CD45+, PAX5+, OCT2+, CD15-, CD30-. Заключение: NLPBCL."),
    },
    "hcl": {
        "age": (55, 11, 30, 85), "male": 0.8, "response_basis": "laboratory",
        "watch_if": lambda f: f["hcl_indication"] == "no",
        "stage": {}, "ecog": {0: 50, 1: 40, 2: 10},
        "fields": lambda rng, ctx: {
            "braf": _maybe(rng, "detected" if rng.random() < 0.92 else "not_detected", 0.1),
            "hcl_indication": "yes" if rng.random() < 0.8 else "no",
            "hcl_variant": "yes" if rng.random() < 0.04 else "no",
            "splenomegaly": "yes" if rng.random() < 0.7 else None,
        },
        "labs": lambda rng, ctx, f: [("8302-2", round(rng.uniform(80, 125)), "g/L"), ("777-3", rng.randint(30, 140), "10*9/L"),
                                     ("751-8", round(rng.uniform(0.3, 1.5), 2), "10*9/L"), ("6690-2", round(rng.uniform(1.2, 4.5), 1), "10*9/L")],
        "first": lambda rng, ctx, f: pick(rng, {("Кладрибин", "кладрибин 0,14 мг/кг × 5 дней", 1): 70,
                                                 ("Кладрибин + ритуксимаб", "кладрибин, ритуксимаб", 1): 20,
                                                 ("Пентостатин", "пентостатин", 12): 10}),
        "duration": (1, 3),
        "later": [("Вемурафениб + ритуксимаб", "вемурафениб, ритуксимаб", None), ("Пентостатин + ритуксимаб", "пентостатин, ритуксимаб", 8),
                  ("Ибрутиниб", "ибрутиниб", None)],
        "p_complete": lambda risk: 0.8, "p_relapse": lambda risk: 0.4,
        "relapse_months": (36, 120), "partial_progress": 0.6, "partial_months": (24, 72), "death_progressive": 0.15,
        "histology": lambda rng, f: ("Трепанобиопсия: интерстициальная инфильтрация клетками с «яичницей-глазуньей», ретикулиновый "
                                     "фиброз. Иммунофенотип: CD19+, CD11c+, CD25+, CD103+, CD123+, аннексин A1+. Заключение: ВКЛ."),
    },
    "mf_ss": {
        "age": (60, 13, 25, 90), "male": 0.6, "watch": 0.05,
        "stage": {"IA": 25, "IB": 30, "IIA": 8, "IIB": 12, "IIIA": 5, "IIIB": 3, "IVA1": 9, "IVA2": 5, "IVB": 3},
        "ecog": {0: 60, 1: 30, 2: 8, 3: 2},
        "fields": _mf_fields, "first": _mf_first,
        "labs": lambda rng, ctx, f: [("6690-2", round(rng.uniform(5, 30) if f["b_cat"] == "B2" else rng.uniform(4, 10), 1), "10*9/L"),
                                     ("2532-0", rng.randint(150, 400), "U/L")],
        "later": [("Метотрексат (низкие дозы)", "метотрексат 25 мг/нед", None), ("Могамулизумаб", "могамулизумаб", None),
                  ("Брентуксимаб ведотин", "брентуксимаб ведотин", 8), ("Гемцитабин", "гемцитабин", 6),
                  ("Локальная ЛТ", "ЛТ на очаги", None)],
        "duration": (3, 9),
        "p_complete": lambda risk: 0.45, "p_relapse": lambda risk: 0.65,
        "relapse_months": (6, 48), "partial_progress": 0.6, "partial_months": (6, 36), "death_progressive": 0.25,
        "histology": lambda rng, f: ("Биопсия кожи: эпидермотропный инфильтрат из атипичных лимфоцитов с церебриформными ядрами, "
                                     "микроабсцессы Потрие. ИГХ: CD3+, CD4+, CD7-, CD8-. Заключение: грибовидный микоз."),
    },
    "tlgll": {
        "age": (62, 12, 25, 90), "male": 0.5, "response_basis": "laboratory",
        "watch_if": lambda f: f["neutropenia"] != "yes" and f["anemia_tx"] != "yes",
        "stage": {}, "ecog": {0: 50, 1: 40, 2: 10},
        "fields": lambda rng, ctx: {
            "stat3": _maybe(rng, "detected" if rng.random() < 0.4 else "not_detected", 0.2),
            "neutropenia": "yes" if rng.random() < 0.45 else "no",
            "anemia_tx": "yes" if rng.random() < 0.15 else "no",
            "autoimmune": "yes" if rng.random() < 0.25 else "no",
            "splenomegaly": "yes" if rng.random() < 0.3 else None,
        },
        "labs": lambda rng, ctx, f: [("751-8", round(rng.uniform(0.1, 0.49) if f["neutropenia"] == "yes" else rng.uniform(0.6, 2.5), 2), "10*9/L"),
                                     ("8302-2", round(rng.uniform(65, 95) if f["anemia_tx"] == "yes" else rng.uniform(100, 145)), "g/L"),
                                     ("731-0", round(rng.uniform(2, 8), 1), "10*9/L")],
        "first": lambda rng, ctx, f: pick(rng, {("Метотрексат", "метотрексат 10 мг/м² в неделю", None): 60,
                                                 ("Циклофосфамид", "циклофосфамид 100 мг/сут", None): 25,
                                                 ("Циклоспорин A", "циклоспорин A", None): 15}),
        "duration": (4, 12),
        "later": [("Циклофосфамид", "циклофосфамид 100 мг/сут", None), ("Циклоспорин A", "циклоспорин A", None),
                  ("Алемтузумаб", "алемтузумаб", None)],
        "p_complete": lambda risk: 0.35, "p_relapse": lambda risk: 0.5,
        "relapse_months": (12, 60), "partial_progress": 0.5, "partial_months": (6, 36), "death_progressive": 0.1,
        "histology": lambda rng, f: ("Проточная цитометрия крови: клональные CD3+ CD8+ CD57+ CD16+ большие гранулярные "
                                     "лимфоциты > 0,5 ×10⁹/л; клональная перестройка TCR. Заключение: T-LGLL."),
    },
}

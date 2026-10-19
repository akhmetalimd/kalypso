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


def _response(rng, criteria: str, complete: bool, line_no: int) -> dict:
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
    if rng.random() < spec.get("watch", 0.05) * (1.5 - risk):
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
        r_date = end + _months(rng, 0.7, 2)
        if r_date > REFERENCE_DATE:
            break
        complete = rng.random() < (p_complete if i == 0 else p_complete * 0.6)
        if rng.random() < 0.88:                          # у части линий оценки ответа нет — флаг полноты
            resp = _response(rng, node["response_criteria"], complete, i + 1)
            timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1, **resp}))
        if complete and rng.random() >= spec["p_relapse"](risk):
            progressing = False
            break                                        # стойкая ремиссия
        lo_m, hi_m = spec["relapse_months"] if complete else (1, 8)
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
}

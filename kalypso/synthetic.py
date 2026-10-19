"""Синтетические когорты ХЛЛ, ДВКЛ и ФЛ для разработки и приёмочных тестов.

Записи вносятся через доменные сервисы (как вручную врачом), поэтому проходят
все проверки, аудит и провенанс. Намеренные дефекты: пропущенные маркеры,
коды пропусков, неканонические и нераспознанные единицы, межфилиальные дубликаты,
завершённые линии без оценки ответа. Все ИИН — сгенерированные числа с корректной
контрольной цифрой; к реальным людям не относятся (хранятся в карточке как обычный ИИН).
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import registry_core as core
from kalypso import registry
from kalypso.db import Database
from kalypso.deid import iin_check_digit
from kalypso.rbac import Principal

REFERENCE_DATE = date(2026, 9, 30)  # «сегодня» синтетики: будущих дат не бывает
REGIMENS = [
    ("FCR", "флударабин, циклофосфамид, ритуксимаб", 6),
    ("BR", "бендамустин, ритуксимаб", 6),
    ("Хлорамбуцил + обинутузумаб", "хлорамбуцил, обинутузумаб", 6),
    ("Венетоклакс + обинутузумаб", "венетоклакс, обинутузумаб", 12),
    ("Ибрутиниб", "ибрутиниб", None),
    ("Акалабрутиниб", "акалабрутиниб", None),
]
HISTOLOGY = ("Биопсия лимфоузла: диффузная инфильтрация мелкими лимфоцитами с пролиферативными центрами. "
             "ИГХ: CD5+, CD23+, CD20 слабо+, CD10-, cyclin D1-. Ki-67 {ki}%. Заключение: ХЛЛ / ЛМЛ.")


def synthetic_iin(rng: random.Random, birth: date, sex: str) -> str:
    century = {1900: (3, 4), 2000: (5, 6)}[birth.year // 100 * 100]
    digit7 = century[0] if sex == "M" else century[1]
    while True:
        head = f"{birth:%y%m%d}{digit7}{rng.randint(0, 9999):04d}"
        check = iin_check_digit(head)
        if check is not None:
            return head + str(check)


def _d(x: date) -> str:
    return x.isoformat()


def _months(rng, lo, hi) -> timedelta:
    return timedelta(days=int(rng.uniform(lo, hi) * 30.4))


def _pick(rng, weighted: dict):
    return rng.choices(list(weighted), weights=list(weighted.values()))[0]


def generate(db: Database, physicians: list[Principal], n: int = 60,
             seed: int = 42, duplicates: int = 3, nosology: str = "cll_sll") -> dict:
    """n синтетических пациентов нозологии + межфилиальные дубликаты. ХЛЛ, ДВКЛ, ФЛ — свои
    генераторы ниже; остальные — kalypso.synthetic_generic.SPECS."""
    one = {"cll_sll": _one_patient, "dlbcl": _one_dlbcl, "follicular": _one_fl}.get(nosology)
    if one is None:                     # остальные нозологии — общий генератор по описанию
        from kalypso import synthetic_generic

        def one(db, doc, rng):
            return synthetic_generic.one_patient(db, doc, rng, nosology, synthetic_iin)
    rng = random.Random(seed)
    made: list[tuple[str, Principal]] = []
    for _ in range(n):
        doc = rng.choice(physicians)
        made.append((one(db, doc, rng), doc))

    requests = 0
    for iin, doc in rng.sample(made, min(duplicates, len(made))):
        others = [p for p in physicians if p.branch_code != doc.branch_code]
        if not others:
            continue
        res = registry.register_patient(db, rng.choice(others), {"iin": iin})
        requests += res.status == "exists_requested"
    return {"patients": len(made), "access_requests": requests, "iins": [i for i, _ in made]}


def _one_patient(db: Database, doc: Principal, rng: random.Random) -> str:
    dx = date(2015, 1, 1) + timedelta(days=rng.randint(0, 9 * 365))
    age = rng.randint(45, 85)
    sex = rng.choice(["M", "M", "F"])
    birth = date(dx.year - age, rng.randint(1, 12), rng.randint(1, 28))
    iin = synthetic_iin(rng, birth, sex)
    binet = _pick(rng, {"A": 60, "B": 25, "C": 15})
    provenance = "retrospective" if dx < date(2024, 1, 1) else "prospective"

    # --- маркеры (со случайными пропусками и кодами пропуска) ---
    ighv = _pick(rng, {"mutated": 45, "unmutated": 45, "not_done": 7, None: 3})
    tp53 = _pick(rng, {"mutated": 10, "wild_type": 72, "not_done": 12, None: 6})
    fish = {k: _pick(rng, {"detected": p, "not_detected": 85 - p, "not_done": 10, None: 5})
            for k, p in (("del17p", 8), ("del11q", 15), ("tri12", 15), ("del13q", 50))}
    b2m = round(rng.uniform(1.4, 7.5), 1)
    tp53_abn = (True if tp53 == "mutated" or fish["del17p"] == "detected"
                else False if tp53 == "wild_type" and fish["del17p"] == "not_detected" else None)
    ipi = core.cll_ipi(age, "Binet", binet, b2m, ighv, tp53_abn)

    # --- хронология: линии, ответы, прогрессии, смерть ---
    timeline: list[tuple[str, dict]] = []
    t = dx
    treated = rng.random() < {"A": 0.3, "B": 0.7, "C": 0.9}[binet]
    n_lines = rng.choice([1, 1, 2, 2, 3]) if treated else 0
    for i in range(n_lines):
        start = t + _months(rng, 1, 18)
        name, drugs, cycles = rng.choice(REGIMENS)
        ongoing = cycles is None and i == n_lines - 1 and rng.random() < 0.6
        end = None if ongoing else start + _months(rng, 4, 14)
        if start > REFERENCE_DATE or (end and end > REFERENCE_DATE):
            break
        timeline.append(("line", {"regimen": name, "drug": drugs, "cycles": cycles, "start_date": _d(start),
                                  "end_date": _d(end) if end else None,
                                  "stop_reason": None if ongoing else rng.choice(
                                      ["завершён план", "завершён план", "токсичность", "прогрессия"])}))
        if end is None:
            t = start
            break
        if rng.random() < 0.85:  # у части линий оценки ответа нет — флаг полноты
            r_date = end + _months(rng, 0.5, 2)
            if r_date <= REFERENCE_DATE:
                timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1,
                                              "response_value": _pick(rng, {"CR": 25, "PR": 45, "nPR": 5, "SD": 15, "PD": 10}),
                                              "response_basis": _pick(rng, {"ct": 60, "clinical": 30, "morphology": 10})}))
        t = end
        if rng.random() < 0.55:
            prog = end + _months(rng, 6, 40)
            if prog > REFERENCE_DATE:
                break
            timeline.append(("event", {"obs_date": _d(prog), "event_type": "progression"}))
            t = prog
        else:
            break

    last_event = max([dx] + [date.fromisoformat(e.get("obs_date") or e.get("end_date") or e["start_date"])
                             for _, e in timeline])
    death = None
    if rng.random() < 0.15:
        d = last_event + _months(rng, 1, 30)
        if d <= REFERENCE_DATE:
            death = d
    lost = death is None and rng.random() < 0.05
    last_contact = death or min(REFERENCE_DATE, last_event + _months(rng, 0, 24 if lost else 18))

    # --- запись через сервисы ---
    data = {
        "iin": iin, "sex": sex, "birth_date": _d(birth), "who_haem5": "cll_sll",
        "dx_date": _d(dx), "last_contact_date": _d(last_contact), "provenance": provenance,
        "status_last_contact": "lost_to_follow_up" if lost else "alive",
        "ecog": rng.choice([0, 0, 1, 1, 2]), "cirs_score": rng.randint(1, 14),
        "b_symptoms": _pick(rng, {"no": 70, "yes": 20, "unknown": 10}),
        "stage_system": "Binet", "stage_value": binet,
        "first_visit_date": _d(dx - timedelta(days=rng.randint(0, 90))),
        "histology_report": HISTOLOGY.format(ki=rng.randint(5, 30)) if rng.random() < 0.7 else None,
    }
    if ipi and rng.random() < 0.85:
        data.update(index_score=ipi["score"], index_group=ipi["group"])
    else:
        data["index_score__missing"] = "unknown"
    pid = registry.register_patient(db, doc, data).record_id

    for key, val in [("IGHV", ighv), ("TP53", tp53), *fish.items()]:
        if val is None:
            continue
        rec = {"field_key": key, "provenance": provenance, "test_date": _d(dx)}
        rec.update({"missing": val} if val in core.MISSING_CODES else {"value": val})
        registry.add_marker(db, doc, pid, rec)

    labs = [("1952-1", b2m, rng.choice(["mg/L", "ug/mL"])),
            ("731-0", round(rng.uniform(6, 120), 1), "10*9/L"),
            ("6690-2", round(rng.uniform(10, 150), 1), rng.choice(["10*9/L", "10*3/uL"])),
            ("777-3", rng.randint(60, 320), "10*9/L"),
            ("2532-0", rng.randint(150, 600), "U/L")]
    hb = round(rng.uniform(85, 150))
    labs.append(("8302-2", hb / 10, "g/dL") if rng.random() < 0.4 else ("8302-2", hb, "g/L"))
    if rng.random() < 0.05:
        labs.append(("8302-2", hb, "g/pL"))  # опечатка в единице -> флаг ручной проверки
    for loinc, value, unit in labs:
        registry.add_lab(db, doc, pid, {"loinc": loinc, "value": value, "unit": unit,
                                        "obs_date": _d(dx), "provenance": provenance})

    line_ids: dict[int, str] = {}
    for kind, e in timeline:
        if kind == "line":
            res = registry.add_line(db, doc, pid, {**e, "provenance": provenance})
            line_ids[len(line_ids) + 1] = str(res.record_id)
        elif kind == "response":
            registry.add_observation(db, doc, pid, {
                "obs_date": e["obs_date"], "line_id": line_ids.get(e["line_no"]),
                "response_value": e["response_value"], "response_basis": e["response_basis"],
                "provenance": provenance})
        else:
            registry.add_observation(db, doc, pid, {**e, "provenance": provenance})
    if death:
        registry.add_observation(db, doc, pid, {
            "obs_date": _d(death), "event_type": "death",
            "death_cause": _pick(rng, {"lpd_related": 55, "not_related": 30, "unknown": 15}),
            "provenance": provenance})
    return iin


# ---------------------------------------------------------------------------
# ДВКЛ (DLBCL, NOS)
# ---------------------------------------------------------------------------
DLBCL_FIRST = {
    ("R-CHOP", "ритуксимаб, циклофосфамид, доксорубицин, винкристин, преднизолон", 6): 75,
    ("Pola-R-CHP", "полатузумаб ведотин, ритуксимаб, циклофосфамид, доксорубицин, преднизолон", 6): 20,
    ("R-CHOEP", "ритуксимаб, циклофосфамид, доксорубицин, винкристин, этопозид, преднизолон", 6): 5,
}
DLBCL_SALVAGE = [("R-ICE", "ритуксимаб, ифосфамид, карбоплатин, этопозид", 3),
                 ("R-DHAP", "ритуксимаб, дексаметазон, цитарабин, цисплатин", 3),
                 ("R-GemOx", "ритуксимаб, гемцитабин, оксалиплатин", 6),
                 ("Pola-BR", "полатузумаб ведотин, бендамустин, ритуксимаб", 6)]
DLBCL_LATER = [("CAR-T (axi-cel)", "аксикабтаген цилолейсел", 1), ("Глофитамаб", "глофитамаб", 12),
               ("Эпкоритамаб", "эпкоритамаб", None), ("Тафаситамаб + леналидомид", "тафаситамаб, леналидомид", 12)]
HISTOLOGY_DLBCL = ("Биопсия лимфоузла: диффузный рост крупных атипичных лимфоидных клеток, стёртый рисунок. "
                   "ИГХ: CD20+, CD10{cd10}, BCL6+, MUM1{mum1}. Ki-67 {ki}%. Заключение: ДВКЛ, NOS.")
_LDH_RANGE = {"le_uln": (140, 245), "uln_1_3": (260, 740), "gt_3_uln": (760, 1500)}   # ВГН 250 Ед/л


def _pet_response(rng, p_cmr: float) -> tuple[str, int]:
    """Метаболический ответ Lugano 2014 и согласованный с ним балл Deauville."""
    if rng.random() < p_cmr:
        return "CMR", rng.choice([1, 2, 2, 3, 3])
    value = _pick(rng, {"PMR": 45, "NMR": 15, "PMD": 40})
    return value, 5 if value == "PMD" else rng.choice([4, 4, 5])


def _one_dlbcl(db: Database, doc: Principal, rng: random.Random) -> str:
    dx = date(2015, 1, 1) + timedelta(days=rng.randint(0, int(10.5 * 365)))
    age = max(22, min(90, int(rng.gauss(64, 13))))
    sex = "M" if rng.random() < 0.55 else "F"
    birth = date(dx.year - age, rng.randint(1, 12), rng.randint(1, 28))
    iin = synthetic_iin(rng, birth, sex)
    provenance = "retrospective" if dx < date(2024, 1, 1) else "prospective"

    stage = _pick(rng, {"I": 12, "IE": 8, "II": 15, "IIE": 8, "III": 20, "IV": 37})
    adv = stage in ("III", "IV")
    ecog = _pick(rng, {0: 25, 1: 40, 2: 20, 3: 10, 4: 5} if adv else {0: 45, 1: 40, 2: 12, 3: 3})
    ldh = _pick(rng, {"le_uln": 30, "uln_1_3": 50, "gt_3_uln": 20} if adv else {"le_uln": 60, "uln_1_3": 35, "gt_3_uln": 5})
    en_n = _pick(rng, {"0": 10, "1": 40, "2plus": 50} if stage == "IV"
                 else {"1": 85, "2plus": 15} if stage.endswith("E") else {"0": 85, "1": 15})
    en_key = "yes" if (stage == "IV" and rng.random() < 0.55) or rng.random() < 0.03 else "no"
    fields = {
        "coo_hans": _pick(rng, {"GCB": 45, "non_GCB": 45, "not_done": 7, None: 3}),
        "myc_r": _pick(rng, {"detected": 10, "not_detected": 70, "not_done": 15, None: 5}),
        "bcl2_r": _pick(rng, {"detected": 18, "not_detected": 62, "not_done": 15, None: 5}),
        "bcl6_r": _pick(rng, {"detected": 25, "not_detected": 55, "not_done": 15, None: 5}),
        "ldh_uln": ldh if rng.random() < 0.95 else None,
        "extranodal_n": en_n,
        "extranodal_nccn": en_key,
        "bulky": "yes" if rng.random() < 0.25 else "no",
    }
    if rng.random() < 0.15:
        fields["coo_gep"] = _pick(rng, {"GCB": 45, "ABC": 40, "unclassified": 15})
    if rng.random() < 0.4:
        fields["double_expressor"] = "yes" if rng.random() < 0.3 else "no"
    exact_age = (dx - birth).days / 365.25          # как в карточке (registry._age)
    ipi = core.ipi(exact_age, ecog, adv, ldh, en_n == "2plus")
    risk = ipi["score"]
    double_hit = fields["myc_r"] == "detected" and fields["bcl2_r"] == "detected"

    # --- хронология: 1-я линия, ПЭТ-КТ в конце лечения, рецидив/прогрессия, 2-я и 3-я линии ---
    timeline: list[tuple[str, dict]] = []
    progressing = False
    t = dx
    if rng.random() < 0.95:
        first = ("R-mini-CHOP", "ритуксимаб, циклофосфамид, доксорубицин (сниж. дозы), винкристин, преднизолон", 6) \
            if age >= 80 else ("DA-EPOCH-R", "дозоадаптированный EPOCH, ритуксимаб", 6) \
            if fields["myc_r"] == "detected" and rng.random() < 0.5 else _pick(rng, DLBCL_FIRST)
        p_cmr = max(0.3, 0.88 - 0.09 * risk - (0.15 if double_hit else 0))
        plan = [first, rng.choice(DLBCL_SALVAGE if age < 75 else DLBCL_SALVAGE[2:]), rng.choice(DLBCL_LATER)]
        for i, (name, drugs, cycles) in enumerate(plan):
            start = t + _months(rng, 0.3, 1.5)
            end = start + _months(rng, 0.8, 1.5) if name.startswith("CAR-T") else start + _months(rng, 3.5, 5.5)
            if end > REFERENCE_DATE:
                break
            timeline.append(("line", {"regimen": name, "drug": drugs, "cycles": cycles, "start_date": _d(start),
                                      "end_date": _d(end), "stop_reason": "завершён план"}))
            r_date = end + _months(rng, 0.7, 2)
            if r_date > REFERENCE_DATE:
                break
            refractory = False
            if rng.random() < 0.88:                      # у части линий оценки ответа нет — флаг полноты
                if rng.random() < 0.85:
                    value, ds = _pet_response(rng, p_cmr)
                    timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1, "response_value": value,
                                                  "response_basis": "pet_ct", "deauville": ds,
                                                  "pet_ct_report": f"ПЭТ-КТ после {i + 1}-й линии: Deauville {ds}."}))
                    refractory = value in ("NMR", "PMD")
                    complete = value == "CMR"
                else:
                    value = _pick(rng, {"CR": 55, "PR": 25, "SD": 8, "PD": 12})
                    timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1, "response_value": value,
                                                  "response_basis": "ct"}))
                    refractory, complete = value in ("SD", "PD"), value == "CR"
            else:
                complete = rng.random() < p_cmr
            if refractory:
                event_date, kind = r_date, "progression"
            elif complete:
                if rng.random() >= 0.12 + 0.05 * risk:
                    progressing = False
                    break                                # стойкая ремиссия
                event_date, kind = r_date + _months(rng, 4, 36), "relapse"
            else:
                event_date, kind = r_date + _months(rng, 1, 8), "progression"
            if event_date > REFERENCE_DATE:
                break
            timeline.append(("event", {"obs_date": _d(event_date), "event_type": kind}))
            t, progressing = event_date, True
            p_cmr = max(0.2, p_cmr - 0.3)

    last_event = max([dx] + [date.fromisoformat(e.get("obs_date") or e.get("end_date") or e["start_date"])
                             for _, e in timeline])
    death, cause = None, None
    if progressing and rng.random() < 0.75:
        death, cause = last_event + _months(rng, 1, 14), _pick(rng, {"lpd_related": 85, "unknown": 15})
    elif rng.random() < 0.06:
        death, cause = last_event + _months(rng, 6, 60), _pick(rng, {"not_related": 70, "unknown": 30})
    if death and death > REFERENCE_DATE:
        death = None
    lost = death is None and rng.random() < 0.04
    last_contact = death or min(REFERENCE_DATE, last_event + _months(rng, 0, 30 if lost else 20))

    # --- запись через сервисы ---
    gcb = fields["coo_hans"] == "GCB"
    data = {
        "iin": iin, "sex": sex, "birth_date": _d(birth), "who_haem5": "dlbcl",
        "dx_date": _d(dx), "last_contact_date": _d(last_contact), "provenance": provenance,
        "status_last_contact": "lost_to_follow_up" if lost else "alive",
        "ecog": ecog, "cirs_score": rng.randint(0, 16) if age >= 65 and rng.random() < 0.7 else None,
        "b_symptoms": _pick(rng, {"no": 55, "yes": 38, "unknown": 7}),
        "stage_system": "Lugano", "stage_value": stage,
        "first_visit_date": _d(dx - timedelta(days=rng.randint(0, 30))),
        "histology_report": HISTOLOGY_DLBCL.format(cd10="+" if gcb else "-", mum1="-" if gcb else "+",
                                                   ki=rng.randint(40, 95)) if rng.random() < 0.75 else None,
    }
    if data["cirs_score"] is None:
        data["cirs_score__missing"] = "not_done"
    if rng.random() < 0.85:
        data["index_score"] = risk
    else:
        data["index_score__missing"] = "unknown"
    pid = registry.register_patient(db, doc, data).record_id

    for key, val in fields.items():
        if val is None:
            continue
        rec = {"field_key": key, "provenance": provenance, "test_date": _d(dx)}
        rec.update({"missing": val} if val in core.MISSING_CODES else {"value": val})
        registry.add_marker(db, doc, pid, rec)

    lo, hi = _LDH_RANGE[ldh]
    for loinc, value, unit in [("2532-0", rng.randint(lo, hi), "U/L"), ("8302-2", round(rng.uniform(90, 150)), "g/L"),
                               ("777-3", rng.randint(90, 420), "10*9/L"), ("6690-2", round(rng.uniform(3, 12), 1), "10*9/L")]:
        registry.add_lab(db, doc, pid, {"loinc": loinc, "value": value, "unit": unit,
                                        "obs_date": _d(dx), "provenance": provenance})

    line_ids: dict[int, str] = {}
    for kind, e in timeline:
        if kind == "line":
            res = registry.add_line(db, doc, pid, {**e, "provenance": provenance})
            line_ids[len(line_ids) + 1] = str(res.record_id)
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
# Фолликулярная лимфома
# ---------------------------------------------------------------------------
FL_FIRST = {
    ("BR", "бендамустин, ритуксимаб", 6): 45,
    ("R-CHOP", "ритуксимаб, циклофосфамид, доксорубицин, винкристин, преднизолон", 6): 22,
    ("R-CVP", "ритуксимаб, циклофосфамид, винкристин, преднизолон", 8): 10,
    ("Обинутузумаб + бендамустин", "обинутузумаб, бендамустин", 6): 13,
    ("Ритуксимаб (монотерапия)", "ритуксимаб", 4): 10,
}
FL_LATER = [("Леналидомид + ритуксимаб (R²)", "леналидомид, ритуксимаб", 12),
            ("Обинутузумаб + бендамустин", "обинутузумаб, бендамустин", 6),
            ("Мосунетузумаб", "мосунетузумаб", 8), ("Таземетостат", "таземетостат", None),
            ("CAR-T (axi-cel)", "аксикабтаген цилолейсел", 1)]
HISTOLOGY_FL = ("Биопсия лимфоузла: нодулярный рост, фолликулы из центроцитов и центробластов ({cb} в п/зр). "
                "ИГХ: CD20+, CD10+, BCL6+, BCL2+ в фолликулах. Ki-67 {ki}%. Заключение: фолликулярная лимфома.")
_B2M_RANGE = {"le_uln": (1.2, 2.3), "gt_uln": (2.6, 6.0)}          # ВГН 2,4 мг/л


def _one_fl(db: Database, doc: Principal, rng: random.Random) -> str:
    dx = date(2015, 1, 1) + timedelta(days=rng.randint(0, int(10.5 * 365)))
    age = max(28, min(88, int(rng.gauss(60, 11))))
    sex = "F" if rng.random() < 0.52 else "M"
    birth = date(dx.year - age, rng.randint(1, 12), rng.randint(1, 28))
    iin = synthetic_iin(rng, birth, sex)
    exact_age = (dx - birth).days / 365.25                 # как в карточке (registry._age)
    provenance = "retrospective" if dx < date(2024, 1, 1) else "prospective"

    stage = _pick(rng, {"I": 10, "IE": 2, "II": 12, "IIE": 2, "III": 26, "IV": 48})
    adv = stage in ("III", "IV")
    bm = "yes" if stage == "IV" and rng.random() < 0.75 else "no"
    ldh = _pick(rng, {"le_uln": 75, "uln_1_3": 22, "gt_3_uln": 3} if adv else {"le_uln": 90, "uln_1_3": 10})
    b2m_cat = "gt_uln" if rng.random() < (0.45 if adv else 0.15) else "le_uln"
    hb = round(rng.uniform(98, 119)) if rng.random() < (0.22 if adv else 0.05) else round(rng.uniform(121, 155))
    b2m = round(rng.uniform(*_B2M_RANGE[b2m_cat]), 1)
    fields = {
        "grade_icc": _pick(rng, {"1_2": 80, "3A": 17, "3B": 3}),
        "nodal_areas": "gt_4" if adv and rng.random() < 0.6 else "le_4",
        "largest_node": "gt_6" if rng.random() < 0.22 else "le_6",
        "bm_involvement": bm if rng.random() < 0.9 else "not_done",
        "ldh_uln": ldh if rng.random() < 0.95 else None,
        "b2m_uln": b2m_cat,
        "gelf": "yes" if rng.random() < (0.6 if adv else 0.2) else "no",
    }
    fl = core.flipi(exact_age, adv, hb, ldh, fields["nodal_areas"] == "gt_4")
    risk = fl["score"]

    # --- хронология: наблюдение без лечения или 1-я линия; POD24; поздние рецидивы; трансформация ---
    timeline: list[tuple[str, dict]] = []
    t = dx
    if stage in ("I", "II") and rng.random() < 0.5:
        first = ("Лучевая терапия", "ЛТ 24 Гр на зону поражения", None)
        start = dx + _months(rng, 0.5, 2)
    elif fields["gelf"] == "yes":
        first = _pick(rng, FL_FIRST)
        start = dx + _months(rng, 0.3, 2)
    else:
        first = _pick(rng, FL_FIRST) if rng.random() < 0.55 else None       # watch & wait, затем лечение
        start = dx + _months(rng, 8, 60)
    progressing, transformed = False, None
    plan = [first] + rng.sample(FL_LATER, 2) if first else []
    p_early = min(0.4, 0.06 + 0.05 * risk)                                   # POD24 ~ 20 % (Casulo, 2015)
    for i, (name, drugs, cycles) in enumerate(plan):
        if i:
            start = t + _months(rng, 1, 6)
        end = start + (_months(rng, 0.8, 1.5) if name in ("Лучевая терапия", "CAR-T (axi-cel)") else _months(rng, 4, 7))
        if end > REFERENCE_DATE:
            break
        timeline.append(("line", {"regimen": name, "drug": drugs, "cycles": cycles, "start_date": _d(start),
                                  "end_date": _d(end), "stop_reason": "завершён план"}))
        r_date = end + _months(rng, 0.7, 2)
        if r_date > REFERENCE_DATE:
            break
        complete = rng.random() < (0.7 if i == 0 else 0.5)
        if rng.random() < 0.88:                              # у части линий оценки ответа нет — флаг полноты
            if complete:
                value, ds = "CMR", rng.choice([1, 2, 2, 3])
            else:
                value = _pick(rng, {"PMR": 75, "NMR": 15, "PMD": 10})
                ds = 5 if value == "PMD" else rng.choice([4, 4, 5])
            timeline.append(("response", {"obs_date": _d(r_date), "line_no": i + 1, "response_value": value,
                                          "response_basis": "pet_ct", "deauville": ds,
                                          "pet_ct_report": f"ПЭТ-КТ после {i + 1}-й линии: Deauville {ds}."}))
        if i == 0 and rng.random() < p_early:
            event_date = start + _months(rng, 7, 23)          # прогрессия в пределах 24 мес. — POD24
            event_date = max(event_date, r_date + timedelta(days=14))
        elif rng.random() < (0.45 if i == 0 else 0.6):
            event_date = r_date + _months(rng, 18 if i == 0 else 4, 90 if i == 0 else 30)
        else:
            progressing = False
            break
        if event_date > REFERENCE_DATE:
            break
        timeline.append(("event", {"obs_date": _d(event_date), "event_type": "progression" if i == 0 else "relapse"}))
        if transformed is None and rng.random() < 0.12:
            transformed = event_date
        t, progressing = event_date, True

    last_event = max([dx] + [date.fromisoformat(e.get("obs_date") or e.get("end_date") or e["start_date"])
                             for _, e in timeline])
    death, cause = None, None
    if progressing and rng.random() < (0.5 if transformed else 0.25):
        death, cause = last_event + _months(rng, 2, 30), _pick(rng, {"lpd_related": 80, "unknown": 20})
    elif rng.random() < 0.05:
        death, cause = last_event + _months(rng, 6, 72), _pick(rng, {"not_related": 75, "unknown": 25})
    if death and death > REFERENCE_DATE:
        death = None
    lost = death is None and rng.random() < 0.04
    last_contact = death or min(REFERENCE_DATE, last_event + _months(rng, 3, 30 if lost else 24))

    # --- запись через сервисы ---
    data = {
        "iin": iin, "sex": sex, "birth_date": _d(birth), "who_haem5": "follicular",
        "dx_date": _d(dx), "last_contact_date": _d(last_contact), "provenance": provenance,
        "status_last_contact": "lost_to_follow_up" if lost else "alive",
        "ecog": _pick(rng, {0: 55, 1: 35, 2: 8, 3: 2}),
        "b_symptoms": _pick(rng, {"no": 75, "yes": 20, "unknown": 5}),
        "stage_system": "Lugano", "stage_value": stage,
        "first_visit_date": _d(dx - timedelta(days=rng.randint(0, 45))),
        "histology_report": HISTOLOGY_FL.format(cb={"1_2": "0–15", "3A": "> 15", "3B": "сплошные поля"}[fields["grade_icc"]],
                                                ki=rng.randint(5, 40)) if rng.random() < 0.75 else None,
    }
    if age >= 65 and rng.random() < 0.7:
        data["cirs_score"] = rng.randint(0, 14)
    else:
        data["cirs_score__missing"] = "not_done"
    if rng.random() < 0.85:
        data["index_score"] = risk
    else:
        data["index_score__missing"] = "unknown"
    pid = registry.register_patient(db, doc, data).record_id

    for key, val in fields.items():
        if val is None:
            continue
        rec = {"field_key": key, "provenance": provenance, "test_date": _d(dx)}
        rec.update({"missing": val} if val in core.MISSING_CODES else {"value": val})
        registry.add_marker(db, doc, pid, rec)
    if transformed:
        registry.add_marker(db, doc, pid, {"field_key": "transformation", "value": "yes", "provenance": provenance,
                                           "test_date": _d(transformed)})

    lo, hi = _LDH_RANGE[ldh]
    for loinc, value, unit in [("8302-2", hb, "g/L"), ("1952-1", b2m, "mg/L"), ("2532-0", rng.randint(lo, hi), "U/L"),
                               ("777-3", rng.randint(120, 380), "10*9/L")]:
        registry.add_lab(db, doc, pid, {"loinc": loinc, "value": value, "unit": unit,
                                        "obs_date": _d(dx), "provenance": provenance})

    line_ids: dict[int, str] = {}
    for kind, e in timeline:
        if kind == "line":
            res = registry.add_line(db, doc, pid, {**e, "provenance": provenance})
            line_ids[len(line_ids) + 1] = str(res.record_id)
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

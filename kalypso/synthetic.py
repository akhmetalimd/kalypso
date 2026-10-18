"""Синтетическая когорта ХЛЛ для разработки и приёмочных тестов.

Записи вносятся через доменные сервисы (как вручную врачом), поэтому проходят
все проверки, аудит и провенанс. Намеренные дефекты: пропущенные маркеры,
коды пропусков, неканонические и нераспознанные единицы, межфилиальные дубликаты,
завершённые линии без оценки ответа. Все ИИН — сгенерированные числа с корректной
контрольной цифрой; к реальным людям не относятся и нигде не сохраняются.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import registry_core as core
from kalypso import registry
from kalypso.db import Database
from kalypso.deid import IinHasher, iin_check_digit
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


def generate(db: Database, hasher: IinHasher, physicians: list[Principal], n: int = 60,
             seed: int = 42, duplicates: int = 3) -> dict:
    rng = random.Random(seed)
    made: list[tuple[str, Principal]] = []
    for _ in range(n):
        doc = rng.choice(physicians)
        made.append((_one_patient(db, hasher, doc, rng), doc))

    requests = 0
    for iin, doc in rng.sample(made, min(duplicates, len(made))):
        others = [p for p in physicians if p.branch_code != doc.branch_code]
        if not others:
            continue
        res = registry.register_patient(db, hasher, rng.choice(others), {"iin": iin})
        requests += res.status == "exists_requested"
    return {"patients": len(made), "access_requests": requests, "iins": [i for i, _ in made]}


def _one_patient(db: Database, hasher: IinHasher, doc: Principal, rng: random.Random) -> str:
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
    pid = registry.register_patient(db, hasher, doc, data).record_id

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

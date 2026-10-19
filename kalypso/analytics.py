"""Аналитика П3: когорта, «Исследователь выживаемости», дашборды.

Когорта загружается в пределах прав пользователя (сервис + RLS): врач видит свой
филиал, куратор/аналитик/админ — все. Расчёты — функциями registry_core
(endpoint, km_estimate, dohner_group), чтобы выгрузка считала так же.

Методические ограничения (PRD, README):
- без p-value / log-rank / Cox — сравнение групп на выгрузке;
- порог лабораторного показателя — внешний (литература), не подбирается здесь;
- группирующий маркер/анализ — только измеренный не позже окна (90 дней) после
  начала отсчёта; иначе «не определено на старте» (защита от immortal time bias);
- признак, который появляется позже старта (Deauville после 1-й линии, POD24 при ФЛ), — только
  анализом от ориентира (landmark): отсчёт от ориентира, события и цензура до него — вне анализа;
- нозологии не смешиваются: нозолого-специфичная группировка сужает когорту до своей нозологии.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

import registry_core as core
from kalypso.charts import DAYS_PER_MONTH, MAX_SERIES, km_ticks
from kalypso.db import Database
from kalypso.i18n import label
from kalypso.rbac import Principal, visible_patients_sql
from kalypso.registry import _marker_map, _tp53_abnormal, active_nodes, computed_indices, pod24_status

WINDOW_DAYS = 90
LAB_LOOKBACK_DAYS = 180
UNDEFINED = "не определено на старте"
GROUPINGS = {
    "none": "Вся когорта",
    "IGHV": "IGHV",
    "TP53": "TP53 / del(17p) (аберрация)",
    "del17p": "del(17p), FISH",
    "dohner": "Иерархия FISH (Döhner)",
    "binet": "Стадия Binet",
    "cll_ipi": "Группа CLL-IPI (в карточке)",
    "line1_regimen": "Режим 1-й линии",
    "sex": "Пол",
    "branch": "Филиал",
    "lab": "Лабораторный показатель (порог)",
    "ipi": "Группа IPI (в карточке)",
    "r_ipi": "R-IPI (расчёт по данным)",
    "nccn_ipi": "NCCN-IPI (расчёт по данным)",
    "coo_hans": "Клеточное происхождение (Hans)",
    "myc_bcl2": "Перестройки MYC / BCL2",
    "lugano": "Стадия Lugano (I–II / III–IV)",
    "bulky": "Bulky-очаг",
    "deauville_eot1": "Deauville после 1-й линии (анализ от ориентира)",
    "flipi": "Группа FLIPI (в карточке)",
    "flipi2": "FLIPI-2 (расчёт по данным)",
    "prima_pi": "PRIMA-PI (расчёт по данным)",
    "grade_icc": "Грейд ФЛ (ICC)",
    "gelf": "Критерии GELF",
    "pod24": "POD24 (анализ от ориентира — 24 мес. от начала 1-й линии)",
    "deauville_pet2": "Deauville промежуточной ПЭТ-КТ (ПЭТ-2, анализ от ориентира)",
}
# Особые группировки нозологий на общей основе (добавляются к автоматическим).
EXTRA_GROUPINGS = {"hodgkin": ("deauville_pet2",)}
# Группировки по нозологиям: выбор такой группировки сужает когорту до её нозологии.
GROUPING_SETS = {
    None: ("none", "sex", "branch", "line1_regimen", "lab"),
    "cll_sll": ("IGHV", "TP53", "del17p", "dohner", "binet", "cll_ipi"),
    "dlbcl": ("ipi", "r_ipi", "nccn_ipi", "coo_hans", "myc_bcl2", "lugano", "bulky", "deauville_eot1"),
    "follicular": ("flipi", "flipi2", "prima_pi", "grade_icc", "lugano", "gelf", "pod24"),
}
STORED_INDEX = {"cll_ipi": "CLL-IPI", "ipi": "IPI", "flipi": "FLIPI"}           # группа индекса карточки
COMPUTED_INDEX = {"r_ipi": "R-IPI", "nccn_ipi": "NCCN-IPI", "flipi2": "FLIPI-2", "prima_pi": "PRIMA-PI"}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _auto_groupings() -> None:
    """Нозологии без ручного набора группировок (MCL, ЛХ, …): индекс карточки, расчётные индексы
    (kalypso.indices.CALCULATORS), стадия Lugano и каждое поле модуля."""
    from kalypso.indices import CALCULATORS

    for node in active_nodes():
        nos = node["id"]
        if nos in GROUPING_SETS:
            continue
        codes = []
        for name in CALCULATORS.get(nos, (None, ()))[1]:
            code = _slug(name)
            if name == node.get("index"):
                STORED_INDEX.setdefault(code, name)
                GROUPINGS.setdefault(code, f"Группа {name} (в карточке)")
            else:
                COMPUTED_INDEX.setdefault(code, name)
                GROUPINGS.setdefault(code, f"{name} (расчёт по данным)")
            codes.append(code)
        if "Lugano" in node.get("staging", []):
            codes.append("lugano")
        for f in node.get("core_fields", []):
            GROUPINGS.setdefault(f["key"], f["label_ru"])
            codes.append(f["key"])
        GROUPING_SETS[nos] = tuple(dict.fromkeys(codes + list(EXTRA_GROUPINGS.get(nos, ()))))


_auto_groupings()
GROUPING_NOSOLOGIES: dict[str, set[str]] = defaultdict(set)
for _nos, _gs in GROUPING_SETS.items():
    for _g in _gs:
        if _nos:
            GROUPING_NOSOLOGIES[_g].add(_nos)
# Без окна «на старте»: признак карточки или ориентир.
NOT_BASELINE = {"none", "sex", "branch", "binet", "lugano", "line1_regimen", "deauville_eot1", "pod24",
                "deauville_pet2", *STORED_INDEX}
DOHNER_LABELS = {"del17p": "del(17p)", "del11q": "del(11q)", "tri12": "+12", "normal": "нормальный",
                 "del13q_sole": "del(13q) одна", "unknown": UNDEFINED}


# ---------------------------------------------------------------------------
# Когорта
# ---------------------------------------------------------------------------
def parse_filters(q: dict) -> dict:
    def year(v):
        try:
            return int(v) if v else None
        except ValueError:
            return None
    nosology = q.get("nosology") or None
    return {"branch": q.get("branch") or None, "year_from": year(q.get("year_from")),
            "year_to": year(q.get("year_to")), "sex": q.get("sex") or None,
            "binet": q.get("binet") or None, "provenance": q.get("provenance") or None,
            "nosology": nosology if nosology in {n["id"] for n in active_nodes()} else None}


def load_cohort(db: Database, p: Principal, filters: dict | None = None) -> list[dict]:
    f = filters or {}
    cond, params = visible_patients_sql(p)
    where = ["p.deleted_at IS NULL", cond]
    for col, key in (("p.branch", "branch"), ("p.sex", "sex"), ("p.provenance", "provenance"),
                     ("p.who_haem5", "nosology")):
        if f.get(key):
            where.append(f"{col} = %s")
            params.append(f[key])
    if f.get("binet"):
        where.append("p.stage_system = 'Binet' AND p.stage_value = %s")
        params.append(f["binet"])
    if f.get("year_from"):
        where.append("extract(year FROM p.dx_date) >= %s")
        params.append(f["year_from"])
    if f.get("year_to"):
        where.append("extract(year FROM p.dx_date) <= %s")
        params.append(f["year_to"])
    with db.tx(p.user_id) as cur:
        cur.execute(f"""SELECT p.patient_id, p.branch, p.sex, p.birth_date, p.who_haem5, p.dx_date, p.dx_date_precision,
                               p.last_contact_date, p.last_contact_precision, p.status_last_contact, p.provenance,
                               p.ecog, p.b_symptoms, p.stage_system, p.stage_value, p.index_name, p.index_score, p.index_group,
                               p.entry_date, p.created_by
                        FROM patient p WHERE {' AND '.join(where)}""", params)
        patients = {r["patient_id"]: {**r, "lines": [], "observations": [], "markers": [], "labs": []}
                    for r in cur.fetchall()}
        ids = list(patients)
        for table, key, order in (("therapy_line", "lines", "line_no"), ("observation", "observations", "obs_date"),
                                  ("module_data", "markers", "test_date"), ("lab", "labs", "obs_date")):
            cur.execute(f"SELECT * FROM {table} WHERE patient_id = ANY(%s) AND deleted_at IS NULL ORDER BY {order}",
                        (ids,))
            for r in cur.fetchall():
                patients[r["patient_id"]][key].append(r)
    return list(patients.values())


# ---------------------------------------------------------------------------
# Значения «на старте» (защита от immortal time bias)
# ---------------------------------------------------------------------------
def baseline_markers(markers: list[dict], t0: date, window: int = WINDOW_DAYS) -> dict[str, str]:
    """Последний результат каждого маркера с датой теста ≤ t0 + окно. Без даты — не используется."""
    limit = t0 + timedelta(days=window)
    usable = [m for m in markers if m["test_date"] and core.impute_date(m["test_date"], m["test_date_precision"]) <= limit]
    return _marker_map(usable)


def baseline_lab(labs: list[dict], loinc: str, t0: date, window: int = WINDOW_DAYS) -> float | None:
    """Каноническое значение, ближайшее к t0, в окне [t0 − 180 дн.; t0 + окно]."""
    lo, hi = t0 - timedelta(days=LAB_LOOKBACK_DAYS), t0 + timedelta(days=window)
    cands = [l for l in labs if l["loinc"] == loinc and l["value_canonical"] is not None and lo <= l["obs_date"] <= hi]
    if not cands:
        return None
    return float(min(cands, key=lambda l: abs((l["obs_date"] - t0).days))["value_canonical"])


def eot_pet(pt: dict, line_no: int = 1) -> tuple[date, int] | None:
    """Первая ПЭТ-КТ с баллом Deauville после окончания линии N (оценка в конце лечения)."""
    line = next((l for l in pt["lines"] if l["line_no"] == line_no), None)
    if line is None or not line["end_date"]:
        return None
    end = core.impute_date(line["end_date"], line["end_precision"])
    pets = sorted((core.impute_date(o["obs_date"], o["obs_precision"]), o["deauville"]) for o in pt["observations"]
                  if o["line_id"] == line["line_id"] and o["deauville"] is not None)
    return next(((d, ds) for d, ds in pets if d >= end), None)


def _deauville_landmark(pt: dict) -> tuple[date, str] | None:
    pet = eot_pet(pt)
    return None if pet is None else (pet[0], "Deauville 1–3" if pet[1] <= 3 else "Deauville 4–5")


def interim_pet(pt: dict, line_no: int = 1) -> tuple[date, int] | None:
    """Первая ПЭТ-КТ с баллом Deauville во время линии N — после старта и до окончания (ПЭТ-2)."""
    line = next((l for l in pt["lines"] if l["line_no"] == line_no), None)
    if line is None or not line["start_date"]:
        return None
    start = core.impute_date(line["start_date"], line["start_precision"])
    end = core.impute_date(line["end_date"], line["end_precision"]) if line["end_date"] else None
    pets = sorted((core.impute_date(o["obs_date"], o["obs_precision"]), o["deauville"]) for o in pt["observations"]
                  if o["line_id"] == line["line_id"] and o["deauville"] is not None)
    return next(((d, ds) for d, ds in pets if d > start and (end is None or d < end)), None)


def _pet2_landmark(pt: dict) -> tuple[date, str] | None:
    pet = interim_pet(pt)
    return None if pet is None else (pet[0], "ПЭТ-2: Deauville 1–3" if pet[1] <= 3 else "ПЭТ-2: Deauville 4–5")


def _pod24_landmark(pt: dict) -> tuple[date, str] | None:
    p = pod24_status(pt, pt["lines"], pt["observations"])
    if p["status"] not in ("pod24", "no_pod24"):
        return None
    return p["landmark"], "POD24" if p["status"] == "pod24" else "без POD24"


# Группировки от ориентира: функция -> (дата ориентира, группа) или None (ориентира нет).
LANDMARKS = {
    "deauville_eot1": (_deauville_landmark,
                       "отсчёт — от первой ПЭТ-КТ с баллом Deauville после окончания 1-й линии"),
    "deauville_pet2": (_pet2_landmark,
                       "отсчёт — от промежуточной ПЭТ-КТ (ПЭТ-2) во время 1-й линии"),
    "pod24": (_pod24_landmark,
              "отсчёт — через 24 мес. от начала 1-й линии иммунохимиотерапии (статус POD24 к этому сроку известен)"),
}


def _index_group(pt: dict, name: str, t0: date) -> str:
    mm = baseline_markers(pt["markers"], t0)
    ix = next((i for i in computed_indices(pt, mm, pt["labs"]) if i["name"] == name), None)
    return label("index_group", ix["result"]["group"]) if ix and ix["result"] else UNDEFINED


def _myc_bcl2(mm: dict[str, str]) -> str:
    neg = {"not_detected", "normal_negative"}
    myc, bcl2 = mm.get("myc_r"), mm.get("bcl2_r")
    if myc == "detected" and bcl2 == "detected":
        return "MYC + BCL2"
    if myc == "detected" and bcl2 in neg:
        return "только MYC"
    if bcl2 == "detected" and myc in neg:
        return "только BCL2"
    if myc in neg and bcl2 in neg:
        return "без перестроек MYC и BCL2"
    return UNDEFINED


def _group(pt: dict, grouping: str, t0: date, lab_loinc: str | None, threshold: float | None) -> str:
    if grouping == "none":
        return "Вся когорта"
    if grouping == "sex":
        return label("sex", pt["sex"])
    if grouping == "branch":
        return pt["branch"]
    if grouping == "binet":
        return f"Binet {pt['stage_value']}" if pt["stage_system"] == "Binet" and pt["stage_value"] else "нет стадии Binet"
    if grouping in STORED_INDEX:
        name = STORED_INDEX[grouping]
        return label("index_group", pt["index_group"]) if pt["index_name"] == name else f"нет {name}"
    if grouping in COMPUTED_INDEX:
        return _index_group(pt, COMPUTED_INDEX[grouping], t0)
    if grouping == "lugano":
        adv = core.advanced_stage(pt["stage_system"], pt["stage_value"])
        return "нет стадии Lugano" if adv is None else ("III–IV" if adv else "I–II")
    if grouping == "line1_regimen":
        line1 = next((l for l in pt["lines"] if l["line_no"] == 1), None)
        return line1["regimen"] if line1 else "без 1-й линии"
    if grouping == "lab":
        v = baseline_lab(pt["labs"], lab_loinc, t0) if lab_loinc else None
        if v is None or threshold is None:
            return UNDEFINED
        return f"> {threshold:g}" if v > threshold else f"≤ {threshold:g}"
    mm = baseline_markers(pt["markers"], t0)
    if grouping == "dohner":
        return DOHNER_LABELS[core.dohner_group(mm)]
    if grouping == "myc_bcl2":
        return _myc_bcl2(mm)
    if grouping == "TP53":
        abn = _tp53_abnormal(mm)
        return UNDEFINED if abn is None else ("аберрация TP53" if abn else "без аберрации TP53")
    value = mm.get(grouping)
    if value is None:
        return UNDEFINED
    if value in core.MISSING_CODES:
        return f"{label('missing', value).lower()}"
    return label("marker_value", value)


# ---------------------------------------------------------------------------
# «Исследователь выживаемости»
# ---------------------------------------------------------------------------
def explore(db: Database, p: Principal, endpoint: str = "OS", line_no: int = 1, grouping: str = "none",
            filters: dict | None = None, lab_loinc: str | None = None, threshold: float | None = None) -> dict:
    p.require("patient.read")
    if endpoint not in core.ENDPOINTS:
        endpoint = "OS"
    if grouping not in GROUPINGS:
        grouping = "none"
    filters = dict(filters or parse_filters({}))
    notes = []
    noses = GROUPING_NOSOLOGIES.get(grouping, set())
    if noses and filters.get("nosology") and filters["nosology"] not in noses:
        grouping = "none"                     # группировка чужой нозологии — не применяем
    elif len(noses) == 1 and not filters.get("nosology"):
        filters["nosology"] = next(iter(noses))
        notes.append(f"Группировка относится к нозологии «{_node_name(filters['nosology'])}» — когорта сужена до неё")
    cohort = load_cohort(db, p, filters)
    if len(noses) > 1 and not filters.get("nosology"):
        cohort = [pt for pt in cohort if pt["who_haem5"] in noses]   # группировка есть у нескольких нозологий
    if not filters.get("nosology") and len({pt["who_haem5"] for pt in cohort}) > 1:
        notes.append("В когорте несколько нозологий — выберите нозологию в фильтре: их выживаемость несопоставима")
    groups: dict[str, list] = defaultdict(list)
    excluded, rows, not_applicable = [], [], 0
    landmark = ({"missing": 0, "dropped": 0, "text": LANDMARKS[grouping][1]}
                if grouping in LANDMARKS else None)
    if grouping == "pod24" and endpoint != "OS":
        notes.append("POD24 — признак для общей выживаемости (OS) от ориентира; для PFS 1-й линии он сам — событие")
    for pt in cohort:
        e = core.endpoint(endpoint, pt, pt["lines"], pt["observations"], line_no)
        if e is None:
            not_applicable += 1
            continue
        if "excluded" in e:
            excluded.append({"patient_id": pt["patient_id"], "reason": e["excluded"]})
            continue
        if landmark is not None:
            mark = LANDMARKS[grouping][0](pt)
            if mark is None:
                landmark["missing"] += 1
                continue
            lm, g = mark
            if lm > e["start"]:               # ориентир позже начала отсчёта — отсчёт от ориентира
                if e["end"] <= lm:
                    landmark["dropped"] += 1  # событие или цензура до ориентира — вне анализа
                    continue
                e = {**e, "start": lm, "days": (e["end"] - lm).days}
        else:
            g = _group(pt, grouping, e["start"], lab_loinc, threshold)
        groups[g].append(e)
        rows.append({"patient_id": pt["patient_id"], "group": g, **e})

    # Порядок: по размеру; «не определено» и служебные — в конце; лишнее — в «прочие».
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == UNDEFINED, -len(kv[1]), kv[0]))
    if len(ordered) > MAX_SERIES:
        head, tail = ordered[:MAX_SERIES - 1], ordered[MAX_SERIES - 1:]
        ordered = head + [("прочие", [e for _, es in tail for e in es])]
        for r in rows:
            if r["group"] in {k for k, _ in tail}:
                r["group"] = "прочие"
    result_groups = []
    for name, es in ordered:
        km = core.km_estimate([e["days"] for e in es], [e["event"] for e in es])
        med = km["median"]
        lo, hi = km["median_ci"]
        result_groups.append({
            "label": name, "km": km, "n": km["n"], "events": km["events"], "durations": [e["days"] for e in es],
            "median_months": None if med is None else round(med / DAYS_PER_MONTH, 1),
            "median_ci_months": tuple(None if v is None else round(v / DAYS_PER_MONTH, 1) for v in (lo, hi)),
        })
        if km["n"] < 10 or km["events"] < 5:
            notes.append(f"Группа «{name}»: n={km['n']}, событий {km['events']} — оценки неустойчивы")
    ticks = km_ticks(result_groups) if result_groups else []
    for g in result_groups:
        g["at_risk"] = core.at_risk_table(g.pop("durations"), [t * DAYS_PER_MONTH for t in ticks])
    return {"endpoint": endpoint, "line_no": line_no, "grouping": grouping, "groups": result_groups,
            "ticks": ticks, "excluded": excluded, "not_applicable": not_applicable, "notes": notes,
            "rows": rows, "n_cohort": len(cohort), "lab_loinc": lab_loinc, "threshold": threshold,
            "window_days": WINDOW_DAYS, "landmark": landmark, "nosology": filters.get("nosology"),
            "baseline_grouping": grouping not in NOT_BASELINE}


def _node_name(node_id: str) -> str:
    return (core.find_node(core.NOSOLOGY_TREE, node_id) or {}).get("name", node_id)


def explore_csv(result: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["patient_id", "endpoint", "line_no", "group", "start", "end", "days", "event", "reason"])
    for r in result["rows"]:
        w.writerow([r["patient_id"], result["endpoint"], result["line_no"] if result["endpoint"] in core.LINE_ENDPOINTS else "",
                    r["group"], r["start"], r["end"], r["days"], r["event"], r["reason"]])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Дашборды
# ---------------------------------------------------------------------------
def _dist(values, order=None, muted=()) -> list[tuple[str, int, bool]]:
    c = Counter(values)
    keys = order or sorted(c, key=lambda k: (k in muted, -c[k]))   # результаты, затем пропуски
    return [(k, c[k], k in muted) for k in keys if c[k]]


def overview(db: Database, p: Principal, filters: dict | None = None, today: date | None = None) -> dict:
    p.require("patient.read")
    today = today or date.today()
    cohort = load_cohort(db, p, filters)
    n = len(cohort)
    with db.tx(p.user_id) as cur:
        ids = [pt["patient_id"] for pt in cohort]
        cur.execute("SELECT patient_id, count(*) AS n FROM patient_branch WHERE patient_id = ANY(%s) GROUP BY 1", (ids,))
        multi = sum(1 for r in cur.fetchall() if r["n"] > 1)
        cur.execute("""SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS month,
                              count(*) AS records, count(DISTINCT created_by) AS authors
                       FROM (SELECT created_at, created_by, patient_id FROM therapy_line
                             UNION ALL SELECT created_at, created_by, patient_id FROM observation
                             UNION ALL SELECT created_at, created_by, patient_id FROM lab
                             UNION ALL SELECT created_at, created_by, patient_id FROM module_data) r
                       WHERE patient_id = ANY(%s) AND created_at > now() - interval '12 months'
                       GROUP BY 1 ORDER BY 1""", (ids,))
        activity = cur.fetchall()
    complete = 0
    for pt in cohort:
        flags = core.completeness_flags({"who_haem5": pt["who_haem5"], "status_last_contact": pt["status_last_contact"],
                                         "module_data": _marker_map(pt["markers"]), "therapy_lines": pt["lines"],
                                         "observations": pt["observations"]})
        complete += not flags
    current = sum(1 for pt in cohort if pt["status_last_contact"] == "dead"
                  or (today - pt["last_contact_date"]).days <= 365)
    node_name = lambda nid: (core.find_node(core.NOSOLOGY_TREE, nid) or {}).get("name", nid)  # noqa: E731
    return {
        "n": n,
        "tiles": {
            "complete": (complete, n), "followup": (current, n), "multi_branch": (multi, n),
            "dead": (sum(pt["status_last_contact"] == "dead" for pt in cohort), n),
            "lost": (sum(pt["status_last_contact"] == "lost_to_follow_up" for pt in cohort), n),
        },
        "by_nosology": _dist([node_name(pt["who_haem5"]) for pt in cohort]),
        "by_branch": _dist([pt["branch"] for pt in cohort]),
        "by_year": sorted(Counter(str(pt["dx_date"].year) for pt in cohort).items()),
        "by_status": _dist([label("status_last_contact", pt["status_last_contact"]) for pt in cohort]),
        "by_provenance": _dist([label("provenance", pt["provenance"]) for pt in cohort]),
        "activity": activity,
        "activity_cols": [(a["month"], a["records"]) for a in activity],
    }


# Ответ — к четырём группам (iwCLL 2018 и Lugano 2014: метаболический — к своему аналогу).
_RESPONSE_MAP = {"CR": "CR", "CRi": "CR", "CMR": "CR", "VGPR": "CR", "PR": "PR", "nPR": "PR", "PR-L": "PR",
                 "PMR": "PR", "MR": "MR", "SD": "SD", "NMR": "SD", "PD": "PD", "PMD": "PD"}
RESPONSE_GROUPS = {
    "cll_sll": [("CR", "CR / CRi"), ("PR", "PR / nPR / PR-L"), ("SD", "SD"), ("PD", "PD")],
    "dlbcl": [("CR", "CR / CMR"), ("PR", "PR / PMR"), ("SD", "SD / NMR"), ("PD", "PD / PMD")],
}
RESPONSE_GROUPS["follicular"] = RESPONSE_GROUPS["dlbcl"]    # тоже Lugano 2014
RESPONSE_GROUPS_BY_CRITERIA = {"iwCLL 2018": RESPONSE_GROUPS["cll_sll"], "Lugano 2014": RESPONSE_GROUPS["dlbcl"],
                               "IWWM-11": [("CR", "CR / VGPR"), ("PR", "PR"), ("MR", "MR"), ("SD", "SD"), ("PD", "PD")]}


def response_groups(nosology: str) -> list:
    node = core.find_node(core.NOSOLOGY_TREE, nosology) or {}
    return RESPONSE_GROUPS.get(nosology) or RESPONSE_GROUPS_BY_CRITERIA.get(node.get("response_criteria"),
                                                                            RESPONSE_GROUPS["dlbcl"])
NOT_ENTERED = "Не внесено"


def _nosology_cohort(db: Database, p: Principal, filters: dict | None, nosology: str) -> list[dict]:
    return load_cohort(db, p, {**(filters or {}), "nosology": nosology})


def _marker_dist(cohort: list[dict], key: str):
    missing_labels = {label("missing", c) for c in core.MISSING_CODES} | {NOT_ENTERED}
    vals = []
    for pt in cohort:
        v = _marker_map(pt["markers"]).get(key)
        vals.append(NOT_ENTERED if v is None else label("missing" if v in core.MISSING_CODES else "marker_value", v))
    return _dist(vals, muted=missing_labels)


def _stored_vs_computed(cohort: list[dict], name: str) -> list:
    agree = Counter()
    for pt in cohort:
        ix = next((i for i in computed_indices(pt, _marker_map(pt["markers"]), pt["labs"]) if i["name"] == name), None)
        c = ix["result"] if ix else None
        stored = pt["index_score"] if pt["index_name"] == name else None
        agree["совпадает" if c and stored is not None and c["score"] == stored else
              "расходится" if c and stored is not None else
              "только в карточке" if stored is not None else
              "только расчёт" if c else "нет данных"] += 1
    return _dist(list(agree.elements()), muted={"нет данных"})


def _computed_dist(cohort: list[dict], name: str, groups: list[str]) -> list:
    vals = []
    for pt in cohort:
        ix = next((i for i in computed_indices(pt, _marker_map(pt["markers"]), pt["labs"]) if i["name"] == name), None)
        vals.append(label("index_group", ix["result"]["group"]) if ix and ix["result"] else "недостаточно данных")
    return _dist(vals, order=[label("index_group", g) for g in groups] + ["недостаточно данных"],
                 muted={"недостаточно данных"})


def _stored_dist(cohort: list[dict], name: str, groups: list[str]) -> list:
    none = f"нет {name}"
    return _dist([label("index_group", pt["index_group"]) if pt["index_name"] == name else none for pt in cohort],
                 order=[label("index_group", g) for g in groups] + [none], muted={none})


def _therapy(cohort: list[dict]) -> dict:
    """Общее для дашбордов нозологий: линии, режимы 1-й линии, ответ по линиям, смерти."""
    n = len(cohort)
    lines_per = Counter(min(len(pt["lines"]), 3) for pt in cohort)
    regimen1 = _dist([next((l["regimen"] for l in pt["lines"] if l["line_no"] == 1), None) for pt in cohort
                      if any(l["line_no"] == 1 for l in pt["lines"])])
    resp_rows = []
    for k in (1, 2, 3):
        counts = Counter()
        for pt in cohort:
            line = next((l for l in pt["lines"] if l["line_no"] == k), None)
            if not line:
                continue
            resp = [o for o in pt["observations"] if o["line_id"] == line["line_id"] and o["response_value"]]
            if resp:
                counts[_RESPONSE_MAP[resp[-1]["response_value"]]] += 1
        if counts:
            resp_rows.append((f"Линия {k}", dict(counts)))
    deaths = [o for pt in cohort for o in pt["observations"] if o["event_type"] == "death"]
    return {
        "treated": (sum(1 for pt in cohort if pt["lines"]), n),
        "lines_per": [("0 линий", lines_per[0], False), ("1 линия", lines_per[1], False),
                      ("2 линии", lines_per[2], False), ("3 и более", lines_per[3], False)],
        "regimen1": regimen1,
        "responses": resp_rows,
        "deaths": _dist([label("death_cause", o["death_cause"]) for o in deaths]),
    }


def _median_months(cohort: list[dict], name: str) -> tuple[float | None, int]:
    es = [core.endpoint(name, pt, pt["lines"], pt["observations"]) for pt in cohort]
    es = [e for e in es if e and "excluded" not in e]
    km = core.km_estimate([e["days"] for e in es], [e["event"] for e in es]) if es else None
    med = None if not km or km["median"] is None else round(km["median"] / DAYS_PER_MONTH, 1)
    return med, len(es)


def cll_dashboard(db: Database, p: Principal, filters: dict | None = None) -> dict:
    p.require("patient.read")
    cohort = _nosology_cohort(db, p, filters, "cll_sll")
    groups = [g for _, g in core.INDEXES["CLL-IPI"]["groups"]]
    ttft_median, ttft_n = _median_months(cohort, "TTFT")
    return {
        "n": len(cohort),
        "markers": {key: _marker_dist(cohort, key) for key in ("IGHV", "TP53", "del17p", "del11q", "tri12", "del13q")},
        "dohner": _dist([DOHNER_LABELS[core.dohner_group(_marker_map(pt["markers"]))] for pt in cohort],
                        muted={UNDEFINED}),
        "binet": _dist([f"Binet {pt['stage_value']}" if pt["stage_system"] == "Binet" else "нет стадии Binet"
                        for pt in cohort], order=["Binet A", "Binet B", "Binet C", "нет стадии Binet"],
                       muted={"нет стадии Binet"}),
        "ipi_stored": _stored_dist(cohort, "CLL-IPI", groups),
        "ipi_agree": _stored_vs_computed(cohort, "CLL-IPI"),
        **_therapy(cohort),
        "ttft_median_months": ttft_median,
        "ttft_n": ttft_n,
    }


def dlbcl_dashboard(db: Database, p: Principal, filters: dict | None = None) -> dict:
    p.require("patient.read")
    cohort = _nosology_cohort(db, p, filters, "dlbcl")
    stages = core.find_node(core.NOSOLOGY_TREE, "dlbcl")["stage_values"]["Lugano"]
    group_codes = lambda name: [g for _, g in core.INDEXES[name]["groups"]]  # noqa: E731
    line1 = [pt for pt in cohort if any(l["line_no"] == 1 for l in pt["lines"])]
    cr1 = 0
    for pt in line1:
        lid = next(l["line_id"] for l in pt["lines"] if l["line_no"] == 1)
        resp = [o for o in pt["observations"] if o["line_id"] == lid and o["response_value"]]
        cr1 += bool(resp) and _RESPONSE_MAP[resp[-1]["response_value"]] == "CR"
    eot = [eot_pet(pt) for pt in line1]
    os_median, os_n = _median_months(cohort, "OS")
    return {
        "n": len(cohort),
        "stage": _dist([f"Lugano {pt['stage_value']}" if pt["stage_system"] == "Lugano" else "нет стадии Lugano"
                        for pt in cohort], order=[f"Lugano {s}" for s in stages] + ["нет стадии Lugano"],
                       muted={"нет стадии Lugano"}),
        "ipi_stored": _stored_dist(cohort, "IPI", group_codes("IPI")),
        "ipi_agree": _stored_vs_computed(cohort, "IPI"),
        "r_ipi": _computed_dist(cohort, "R-IPI", group_codes("R-IPI")),
        "nccn_ipi": _computed_dist(cohort, "NCCN-IPI", group_codes("NCCN-IPI")),
        "markers": {key: _marker_dist(cohort, key)
                    for key in ("coo_hans", "myc_r", "bcl2_r", "bcl6_r", "ldh_uln", "extranodal_n", "bulky")},
        "myc_bcl2": _dist([_myc_bcl2(_marker_map(pt["markers"])) for pt in cohort], muted={UNDEFINED}),
        "eot_deauville": _dist([f"Deauville {ds}" if ds else "нет ПЭТ-КТ после 1-й линии"
                                for ds in (e[1] if e else None for e in eot)],
                               order=[f"Deauville {i}" for i in range(1, 6)] + ["нет ПЭТ-КТ после 1-й линии"],
                               muted={"нет ПЭТ-КТ после 1-й линии"}),
        "cr1": (cr1, len(line1)),
        **_therapy(cohort),
        "os_median_months": os_median,
        "os_n": os_n,
    }


POD24_LABELS = {"pod24": "POD24", "no_pod24": "без POD24", "undetermined": "пока не определено",
                "not_applicable": "1-я линия не иммунохимиотерапия / без лечения"}


def fl_dashboard(db: Database, p: Principal, filters: dict | None = None) -> dict:
    p.require("patient.read")
    cohort = _nosology_cohort(db, p, filters, "follicular")
    stages = core.find_node(core.NOSOLOGY_TREE, "follicular")["stage_values"]["Lugano"]
    group_codes = lambda name: [g for _, g in core.INDEXES[name]["groups"]]  # noqa: E731
    pods = [pod24_status(pt, pt["lines"], pt["observations"])["status"] for pt in cohort]
    transformed = sum(1 for pt in cohort if _marker_map(pt["markers"]).get("transformation") == "yes")
    ttft_median, ttft_n = _median_months(cohort, "TTFT")
    os_median, os_n = _median_months(cohort, "OS")
    return {
        "n": len(cohort),
        "stage": _dist([f"Lugano {pt['stage_value']}" if pt["stage_system"] == "Lugano" else "нет стадии Lugano"
                        for pt in cohort], order=[f"Lugano {s}" for s in stages] + ["нет стадии Lugano"],
                       muted={"нет стадии Lugano"}),
        "flipi_stored": _stored_dist(cohort, "FLIPI", group_codes("FLIPI")),
        "flipi_agree": _stored_vs_computed(cohort, "FLIPI"),
        "flipi2": _computed_dist(cohort, "FLIPI-2", group_codes("FLIPI-2")),
        "prima_pi": _computed_dist(cohort, "PRIMA-PI", group_codes("PRIMA-PI")),
        "markers": {key: _marker_dist(cohort, key)
                    for key in ("grade_icc", "nodal_areas", "largest_node", "bm_involvement", "ldh_uln", "b2m_uln", "gelf")},
        "pod24": _dist([POD24_LABELS[s] for s in pods], order=list(POD24_LABELS.values()),
                       muted={POD24_LABELS["undetermined"], POD24_LABELS["not_applicable"]}),
        "pod24_known": (pods.count("pod24"), pods.count("pod24") + pods.count("no_pod24")),
        "watch_wait": (sum(1 for pt in cohort if not pt["lines"]), len(cohort)),
        "transformed": (transformed, len(cohort)),
        **_therapy(cohort),
        "ttft_median_months": ttft_median, "ttft_n": ttft_n,
        "os_median_months": os_median, "os_n": os_n,
    }


def module_dashboard(db: Database, p: Principal, nosology: str, filters: dict | None = None) -> dict:
    """Дашборд нозологии по её узлу дерева: стадии, индексы (карточка, сверка, расчёт), каждое
    поле модуля, терапия, ответ, смерти, TTFT и OS. Для MCL, ЛХ, WM, MZL, PTCL."""
    from kalypso.indices import CALCULATORS

    p.require("patient.read")
    node = core.find_node(core.NOSOLOGY_TREE, nosology)
    cohort = _nosology_cohort(db, p, filters, nosology)
    stages = []
    for system, values in node.get("stage_values", {}).items():
        none = f"нет стадии {system}"
        stages.append((f"Стадия {system}", _dist(
            [f"{system} {pt['stage_value']}" if pt["stage_system"] == system else none for pt in cohort],
            order=[f"{system} {v}" for v in values] + [none], muted={none})))
    indices = []
    for name in CALCULATORS.get(nosology, (None, ()))[1]:
        groups = [g for _, g in core.INDEXES[name]["groups"]]
        if name == node.get("index"):
            indices.append((f"{name} (в карточке)", _stored_dist(cohort, name, groups)))
            indices.append((f"{name}: карточка и расчёт по данным", _stored_vs_computed(cohort, name)))
        else:
            indices.append((f"{name} (расчёт)", _computed_dist(cohort, name, groups)))
    ttft_median, ttft_n = _median_months(cohort, "TTFT")
    os_median, os_n = _median_months(cohort, "OS")
    return {
        "n": len(cohort), "nosology": nosology, "name": node["name"], "criteria": node.get("response_criteria"),
        "stages": stages, "indices": indices,
        "markers": [(f["label_ru"], _marker_dist(cohort, f["key"])) for f in node.get("core_fields", [])],
        "watch_wait": (sum(1 for pt in cohort if not pt["lines"]), len(cohort)),
        **_therapy(cohort),
        "ttft_median_months": ttft_median, "ttft_n": ttft_n,
        "os_median_months": os_median, "os_n": os_n,
    }

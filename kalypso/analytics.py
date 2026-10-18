"""Аналитика П3: когорта, «Исследователь выживаемости», дашборды.

Когорта загружается в пределах прав пользователя (сервис + RLS): врач видит свой
филиал, куратор/аналитик/админ — все. Расчёты — функциями registry_core
(endpoint, km_estimate, dohner_group), чтобы выгрузка считала так же.

Методические ограничения (PRD, README):
- без p-value / log-rank / Cox — сравнение групп на выгрузке;
- порог лабораторного показателя — внешний (литература), не подбирается здесь;
- группирующий маркер/анализ — только измеренный не позже окна (90 дней) после
  начала отсчёта; иначе «не определено на старте» (защита от immortal time bias).
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, timedelta

import registry_core as core
from kalypso.charts import DAYS_PER_MONTH, MAX_SERIES, km_ticks
from kalypso.db import Database
from kalypso.i18n import label
from kalypso.rbac import Principal, visible_patients_sql
from kalypso.registry import _age, _marker_map, _tp53_abnormal

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
}
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
    return {"branch": q.get("branch") or None, "year_from": year(q.get("year_from")),
            "year_to": year(q.get("year_to")), "sex": q.get("sex") or None,
            "binet": q.get("binet") or None, "provenance": q.get("provenance") or None}


def load_cohort(db: Database, p: Principal, filters: dict | None = None) -> list[dict]:
    f = filters or {}
    cond, params = visible_patients_sql(p)
    where = ["p.deleted_at IS NULL", cond]
    for col, key in (("p.branch", "branch"), ("p.sex", "sex"), ("p.provenance", "provenance")):
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
                               p.stage_system, p.stage_value, p.index_name, p.index_score, p.index_group,
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


def _group(pt: dict, grouping: str, t0: date, lab_loinc: str | None, threshold: float | None) -> str:
    if grouping == "none":
        return "Вся когорта"
    if grouping == "sex":
        return label("sex", pt["sex"])
    if grouping == "branch":
        return pt["branch"]
    if grouping == "binet":
        return f"Binet {pt['stage_value']}" if pt["stage_system"] == "Binet" and pt["stage_value"] else "нет стадии Binet"
    if grouping == "cll_ipi":
        return label("index_group", pt["index_group"]) if pt["index_name"] == "CLL-IPI" else "нет CLL-IPI"
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
    cohort = load_cohort(db, p, filters)
    groups: dict[str, list] = defaultdict(list)
    excluded, rows, not_applicable = [], [], 0
    for pt in cohort:
        e = core.endpoint(endpoint, pt, pt["lines"], pt["observations"], line_no)
        if e is None:
            not_applicable += 1
            continue
        if "excluded" in e:
            excluded.append({"patient_id": pt["patient_id"], "reason": e["excluded"]})
            continue
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
    result_groups, notes = [], []
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
            "window_days": WINDOW_DAYS}


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


RESPONSE_GROUPS = [("CR", "CR / CRi"), ("PR", "PR / nPR / PR-L"), ("SD", "SD"), ("PD", "PD")]
_RESPONSE_MAP = {"CR": "CR", "CRi": "CR", "PR": "PR", "nPR": "PR", "PR-L": "PR", "SD": "SD", "PD": "PD"}


def cll_dashboard(db: Database, p: Principal, filters: dict | None = None) -> dict:
    p.require("patient.read")
    cohort = [pt for pt in load_cohort(db, p, filters) if pt["who_haem5"] == "cll_sll"]
    n = len(cohort)
    missing_labels = {label("missing", c) for c in core.MISSING_CODES} | {"Не внесено"}

    def marker_dist(key):
        vals = []
        for pt in cohort:
            v = _marker_map(pt["markers"]).get(key)
            vals.append("Не внесено" if v is None else label("missing" if v in core.MISSING_CODES else "marker_value", v))
        return _dist(vals, muted=missing_labels)

    computed_ipi = {}
    for pt in cohort:
        mm = _marker_map(pt["markers"])
        b2m = next((float(l["value_canonical"]) for l in pt["labs"] if l["loinc"] == "1952-1" and l["value_canonical"] is not None), None)
        computed_ipi[pt["patient_id"]] = core.cll_ipi(_age(pt["birth_date"], pt["dx_date"]), pt["stage_system"],
                                                      pt["stage_value"], b2m, mm.get("IGHV"), _tp53_abnormal(mm))
    ipi_agree = Counter()
    for pt in cohort:
        c, stored = computed_ipi[pt["patient_id"]], pt["index_score"] if pt["index_name"] == "CLL-IPI" else None
        ipi_agree["совпадает" if c and stored is not None and c["score"] == stored else
                  "расходится" if c and stored is not None else
                  "только в карточке" if stored is not None else
                  "только расчёт" if c else "нет данных"] += 1

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
    ttft = [core.endpoint("TTFT", pt, pt["lines"], pt["observations"]) for pt in cohort]
    ttft = [e for e in ttft if e and "excluded" not in e]
    ttft_km = core.km_estimate([e["days"] for e in ttft], [e["event"] for e in ttft]) if ttft else None
    return {
        "n": n,
        "markers": {key: marker_dist(key) for key in ("IGHV", "TP53", "del17p", "del11q", "tri12", "del13q")},
        "dohner": _dist([DOHNER_LABELS[core.dohner_group(_marker_map(pt["markers"]))] for pt in cohort],
                        muted={UNDEFINED}),
        "binet": _dist([f"Binet {pt['stage_value']}" if pt["stage_system"] == "Binet" else "нет стадии Binet"
                        for pt in cohort], order=["Binet A", "Binet B", "Binet C", "нет стадии Binet"],
                       muted={"нет стадии Binet"}),
        "ipi_stored": _dist([label("index_group", pt["index_group"]) if pt["index_name"] == "CLL-IPI" else "нет CLL-IPI"
                             for pt in cohort], order=[label("index_group", g) for g in ("low", "intermediate", "high", "very_high")] + ["нет CLL-IPI"],
                            muted={"нет CLL-IPI"}),
        "ipi_agree": _dist(list(ipi_agree.elements()), muted={"нет данных"}),
        "treated": (sum(1 for pt in cohort if pt["lines"]), n),
        "lines_per": [("0 линий", lines_per[0], False), ("1 линия", lines_per[1], False),
                      ("2 линии", lines_per[2], False), ("3 и более", lines_per[3], False)],
        "regimen1": regimen1,
        "responses": resp_rows,
        "deaths": _dist([label("death_cause", o["death_cause"]) for o in deaths]),
        "ttft_median_months": None if not ttft_km or ttft_km["median"] is None else round(ttft_km["median"] / DAYS_PER_MONTH, 1),
        "ttft_n": len(ttft),
    }

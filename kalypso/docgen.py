"""Автогенерация документов по шаблонам (FR-8, П3) — только из данных регистра, без AI.

Каждый документ — черновик: врач проверяет и редактирует его вне платформы.
Персональных данных в регистре нет, поэтому их нет и в документах (псевдоним ID).
"""
from __future__ import annotations

from datetime import date

import registry_core as core
from kalypso.i18n import flag, label
from kalypso.registry import _age, core_fields

DRAFT_NOTE = "Черновик, сформирован автоматически из данных регистра. Проверьте перед использованием."


def _d(value, precision="day") -> str:
    if not value:
        return "—"
    return {"year": f"{value.year}", "month": f"{value:%m.%Y}"}.get(precision, f"{value:%d.%m.%Y}")


def patient_summary(card: dict) -> dict:
    pt = card["patient"]
    node = core.find_node(core.NOSOLOGY_TREE, pt["who_haem5"]) or {}
    age = _age(pt["birth_date"], pt["dx_date"])
    markers = []
    for f in core_fields(pt["who_haem5"]):
        recs = [m for m in card["markers"] if m["field_key"] == f["key"]]
        last = recs[-1] if recs else None
        if f.get("optional") and not last:
            continue  # дополнительное поле без результата в документ не выводим
        markers.append({"label": f["label_ru"],
                        "value": (label("marker_value", last["value"]) if last["value"]
                                  else label("missing", last["missing"])) if last else "не внесено",
                        "date": _d(last["test_date"], last["test_date_precision"]) if last and last["test_date"] else None})
    lines = []
    for ln in card["lines"]:
        resp = [o for o in card["observations"] if o["line_id"] == ln["line_id"] and o["response_value"]]
        lines.append({"no": ln["line_no"], "regimen": ln["regimen"], "drug": ln["drug"],
                      "start": _d(ln["start_date"], ln["start_precision"]), "end": _d(ln["end_date"], ln["end_precision"]),
                      "stop_reason": ln["stop_reason"], "response": resp[-1]["response_value"] if resp else None})
    events = [{"type": label("event_type", o["event_type"]), "date": _d(o["event_date"]),
               "cause": label("death_cause", o["death_cause"]) if o["death_cause"] else None}
              for o in card["observations"] if o["event_type"] != "none"]
    indices = []
    for ix in card.get("indices", []):
        r = ix["result"]
        stored = (f"{pt['index_score']} ({label('index_group', pt['index_group'])})"
                  if ix["stored"] and pt["index_name"] == ix["name"] else None)
        indices.append({"name": ix["name"], "stored": stored, "show_stored": ix["stored"],
                        "computed": f"{r['score']} ({label('index_group', r['group'])})" if r else None})
    return {
        "pid": str(pt["patient_id"])[:8], "sex": label("sex", pt["sex"]),
        "age_at_dx": None if age is None else int(age),
        "nosology": node.get("name", pt["who_haem5"]), "icd_o": pt["icd_o_3_2"], "mkb": pt["mkb"],
        "dx_date": _d(pt["dx_date"], pt["dx_date_precision"]), "dx_version": pt["dx_version"],
        "stage": f"{pt['stage_system']} {pt['stage_value']}" if pt["stage_system"] else "не указана",
        "ecog": pt["ecog"], "cirs": pt["cirs_score"],
        "indices": indices,
        "markers": markers, "lines": lines, "events": events,
        "status": label("status_last_contact", pt["status_last_contact"]),
        "last_contact": _d(pt["last_contact_date"], pt["last_contact_precision"]),
        "histology": pt["histology_report"],
        "gaps": [flag(f) for f in card["flags"]["completeness"]],
        "quality": [flag(f) for f in card["flags"]["quality"]],
        "generated": f"{date.today():%d.%m.%Y}",
    }


def tumor_board_text(s: dict) -> str:
    lines = [f"Резюме для Tumor Board — пациент {s['pid']} (регистр KALYPSO)", DRAFT_NOTE, "",
             f"{s['sex']}, возраст на момент диагноза: {s['age_at_dx'] if s['age_at_dx'] is not None else 'н/д'}.",
             f"Диагноз: {s['nosology']} (ICD-O-3.2 {s['icd_o']}, МКБ-10 {s['mkb']}), от {s['dx_date']}"
             + (f", версия диагноза {s['dx_version']}" if s["dx_version"] > 1 else "") + ".",
             f"Стадия: {s['stage']}. ECOG: {s['ecog'] if s['ecog'] is not None else 'н/д'}. "
             f"CIRS-G: {s['cirs'] if s['cirs'] is not None else 'н/д'}.",
             *[(f"{ix['name']}: в карточке {ix['stored'] or 'не указан'}; " if ix["show_stored"] else f"{ix['name']}: ")
               + f"расчёт по данным {ix['computed'] or 'недостаточно данных'}." for ix in s["indices"]],
             "", "Маркеры:"]
    lines += [f"  - {m['label']}: {m['value']}" + (f" ({m['date']})" if m["date"] else "") for m in s["markers"]]
    lines += ["", "Терапия:"]
    lines += [f"  - Линия {l['no']}: {l['regimen']}, {l['start']} — {l['end']}"
              + (f", ответ {l['response']}" if l["response"] else ", ответ не оценён")
              + (f", окончание: {l['stop_reason']}" if l["stop_reason"] else "") for l in s["lines"]] or ["  - не проводилась"]
    if s["events"]:
        lines += ["", "События:"] + [f"  - {e['type']} {e['date']}" + (f" ({e['cause']})" if e["cause"] else "") for e in s["events"]]
    lines += ["", f"Статус на {s['last_contact']}: {s['status']}."]
    if s["gaps"]:
        lines += ["", "Не хватает данных для обсуждения:"] + [f"  - {g}" for g in s["gaps"]]
    lines += ["", "Вопрос к консилиуму: ________________________________"]
    return "\n".join(lines)


def epicrisis_text(s: dict) -> str:
    therapy = "; ".join(f"{l['no']}-я линия — {l['regimen']} ({l['start']} — {l['end']})"
                        + (f", ответ {l['response']}" if l["response"] else "") for l in s["lines"]) or "не проводилась"
    markers = ", ".join(f"{m['label']} — {m['value']}" for m in s["markers"] if m["value"] != "не внесено") or "не исследованы"
    events = "; ".join(f"{e['type'].lower()} {e['date']}" for e in s["events"]) or "не зарегистрированы"
    return "\n\n".join([
        f"Эпикриз (черновик) — пациент {s['pid']}", DRAFT_NOTE,
        f"Диагноз: {s['nosology']}, стадия {s['stage']} (ICD-O-3.2 {s['icd_o']}, МКБ-10 {s['mkb']}). "
        f"Диагноз установлен {s['dx_date']}.",
        f"Цитогенетические и молекулярные маркеры: {markers}.",
        f"Проведённая терапия: {therapy}.",
        f"События наблюдения: {events}.",
        f"Состояние на дату последнего контакта ({s['last_contact']}): {s['status'].lower()}.",
        "Рекомендации: ________________________________",
    ])


def cohort_abstract(result: dict, filters_text: str, grouping_label: str) -> str:
    ep = result["endpoint"]
    line = f" (от старта {result['line_no']}-й линии)" if ep in core.LINE_ENDPOINTS else ""
    parts = [
        "Тезисы (черновик). " + DRAFT_NOTE,
        f"Методы. Когорта регистра KALYPSO: {result['n_cohort']} пациентов ({filters_text}). "
        f"Конечная точка — {ep}{line}: {core.ENDPOINTS[ep].split(': ', 1)[-1]}. Оценка Каплана–Майера, "
        f"95 % ДИ — по Гринвуду (log-log), ДИ медианы — по пересечению границ с 50 %. "
        f"Неточные даты заменялись серединой периода. Группирующий признак — {grouping_label.lower()}, "
        f"значение на момент начала отсчёта (окно {result['window_days']} дней); сравнительная статистика "
        f"(log-rank, регрессия Кокса) не выполнялась.",
    ]
    res = []
    for g in result["groups"]:
        med = "не достигнута" if g["median_months"] is None else f"{g['median_months']} мес."
        lo, hi = g["median_ci_months"]
        ci = "" if g["median_months"] is None else f" (95 % ДИ {lo if lo is not None else '—'}–{hi if hi is not None else 'не достигнута'})"
        res.append(f"«{g['label']}»: n={g['n']}, событий {g['events']}, медиана {med}{ci}")
    parts.append("Результаты. " + "; ".join(res) + "."
                 + (f" Исключено {len(result['excluded'])} записей с противоречием дат." if result["excluded"] else ""))
    parts.append("Ограничения. Наблюдательные данные: ассоциации не означают причинности; "
                 "для сравнения групп нужна поправка на факторы риска на выгрузке. Пилот — на синтетических данных.")
    return "\n\n".join(parts)

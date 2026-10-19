"""Индексы по данным карточки и сведения модулей нозологий.

Формулы индексов — в registry_core; здесь — откуда берутся компоненты: поля модуля
(module_data), карточка пациента и анализы, ближайшие к диагнозу. Ручной индекс карточки
(index_name/score/group) сверяется с расчётом того же названия (stored = True).
Нет компонента — нет результата: «норму» молча не подставляем.

Новая нозология: функция расчёта + строка в CALCULATORS (названия индексов —
для группировок аналитики), при необходимости — module_quality / module_info.
"""
from __future__ import annotations

from datetime import date

import registry_core as core

LOINC_B2M = "1952-1"
LOINC_HB = "8302-2"
LOINC_WBC = "6690-2"
LOINC_LYMPH = "731-0"
LOINC_ALBUMIN = "1751-7"
LOINC_PLT = "777-3"
LOINC_LDH = "2532-0"
LOINC_IGM = "2472-0"


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------
def _age(birth: date | None, at: date | None) -> float | None:
    if not birth or not at:
        return None
    return (at - birth).days / 365.25


def _tp53_abnormal(mm: dict[str, str]) -> bool | None:
    tp53, del17p = mm.get("TP53"), mm.get("del17p")
    if tp53 == "mutated" or del17p == "detected":
        return True
    normal = {"wild_type", "not_detected", "normal_negative"}
    if tp53 in normal and del17p in normal:
        return False
    return None


def _yes_no(v: str | None) -> bool | None:
    return {"yes": True, "no": False, "normal_negative": False}.get(v or "")


def _detected(v: str | None) -> bool | None:
    return {"detected": True, "not_detected": False, "normal_negative": False}.get(v or "")


def _ldh(v: str | None) -> str | None:
    return "le_uln" if v == "normal_negative" else v


def _gt(v: str | None, yes: str, no: str) -> bool | None:
    return {yes: True, no: False, "normal_negative": False}.get(v or "")


def _lab_near(labs: list[dict], loinc: str, t0: date, before: int = 180, after: int = 90) -> float | None:
    """Каноническое значение анализа, ближайшее к дате диагноза, в окне [t0 − 180; t0 + 90] дней."""
    cands = [l for l in labs if l["loinc"] == loinc and l["value_canonical"] is not None
             and -before <= (l["obs_date"] - t0).days <= after]
    return float(min(cands, key=lambda l: abs((l["obs_date"] - t0).days))["value_canonical"]) if cands else None


def _extranodal_gt1(v: str | None) -> bool | None:
    return {"0": False, "1": False, "2plus": True, "normal_negative": False}.get(v or "")


def _adv(pt: dict) -> bool | None:
    return core.advanced_stage(pt["stage_system"], pt["stage_value"])


def _ix(name: str, result, needs: str, stored: bool = False) -> dict:
    return {"name": name, "stored": stored, "result": result, "needs": needs}


# ---------------------------------------------------------------------------
# Расчёты по нозологиям
# ---------------------------------------------------------------------------
def _cll(pt, mm, labs, age):
    b2m = next((float(l["value_canonical"]) for l in labs
                if l["loinc"] == LOINC_B2M and l["value_canonical"] is not None), None)
    return [_ix("CLL-IPI", core.cll_ipi(age, pt["stage_system"], pt["stage_value"], b2m, mm.get("IGHV"),
                                        _tp53_abnormal(mm)),
                "TP53/del(17p), IGHV, β2M, стадия, возраст", stored=True)]


def _dlbcl(pt, mm, labs, age):
    adv, ldh = _adv(pt), _ldh(mm.get("ldh_uln"))
    ipi = core.ipi(age, pt["ecog"], adv, ldh, _extranodal_gt1(mm.get("extranodal_n")))
    return [
        _ix("IPI", ipi, "возраст, ECOG, стадия Lugano, ЛДГ, число экстранодальных очагов", stored=True),
        _ix("R-IPI", core.r_ipi(ipi["score"]) if ipi else None, "те же компоненты, что у IPI"),
        _ix("NCCN-IPI", core.nccn_ipi(age, pt["ecog"], adv, ldh, _yes_no(mm.get("extranodal_nccn"))),
            "возраст, ECOG, стадия, ЛДГ, поражение КМ/ЦНС/печени-ЖКТ/лёгкого"),
    ]


def _fl(pt, mm, labs, age):
    hb = _lab_near(labs, LOINC_HB, pt["dx_date"])
    bm = _yes_no(mm.get("bm_involvement"))
    return [
        _ix("FLIPI", core.flipi(age, _adv(pt), hb, _ldh(mm.get("ldh_uln")), _gt(mm.get("nodal_areas"), "gt_4", "le_4")),
            "возраст, стадия Lugano, Hb на диагнозе (анализ), ЛДГ, число зон лимфоузлов", stored=True),
        _ix("FLIPI-2", core.flipi2(age, hb, _gt(mm.get("b2m_uln"), "gt_uln", "le_uln"),
                                   _gt(mm.get("largest_node"), "gt_6", "le_6"), bm),
            "возраст, Hb (анализ), β2M относительно ВГН, наибольший лимфоузел, костный мозг"),
        _ix("PRIMA-PI", core.prima_pi(_lab_near(labs, LOINC_B2M, pt["dx_date"]), bm),
            "β2-микроглобулин на диагнозе (анализ, мг/л), поражение костного мозга"),
    ]


def _mcl(pt, mm, labs, age):
    ratio = "lt_067" if mm.get("ldh_ratio") == "normal_negative" else mm.get("ldh_ratio")
    s = core.smipi(age, pt["ecog"], ratio, _lab_near(labs, LOINC_WBC, pt["dx_date"]))
    ki67 = _gt(mm.get("ki67"), "ge_30", "lt_30")
    return [
        _ix("sMIPI", s, "возраст, ECOG, ЛДГ/ВГН, лейкоциты на диагнозе (анализ)", stored=True),
        _ix("MIPI-c", core.mipi_c(s["group"], ki67) if s else None, "sMIPI и Ki-67"),
    ]


def _hodgkin(pt, mm, labs, age):
    t0 = pt["dx_date"]
    stage = pt["stage_value"] if pt["stage_system"] in ("Lugano", "Ann Arbor") else None
    ips = core.ips(_lab_near(labs, LOINC_ALBUMIN, t0), _lab_near(labs, LOINC_HB, t0),
                   {"M": True, "F": False}.get(pt.get("sex")), age, None if stage is None else stage == "IV",
                   _lab_near(labs, LOINC_WBC, t0), _lab_near(labs, LOINC_LYMPH, t0))
    ghsg = core.ghsg_group(stage, _yes_no(pt.get("b_symptoms")), _yes_no(mm.get("bulky_med")),
                           _yes_no(mm.get("extranodal_hl")), _yes_no(mm.get("esr_raised")),
                           _gt(mm.get("nodal_areas_hl"), "ge_3", "lt_3"))
    return [
        _ix("IPS", ips, "альбумин, Hb, лейкоциты, лимфоциты (анализы на диагнозе), пол, возраст, стадия", stored=True),
        _ix("GHSG", ghsg, "стадия, B-симптомы, средостение, экстранодальное поражение, СОЭ, число зон"),
    ]


def _wm(pt, mm, labs, age):
    def lab(loinc):
        return _lab_near(labs, loinc, pt["dx_date"])
    return [
        _ix("IPSS-WM", core.ipss_wm(age, lab(LOINC_HB), lab(LOINC_PLT), lab(LOINC_B2M), lab(LOINC_IGM)),
            "возраст, Hb, тромбоциты, β2M, IgM (анализы на диагнозе)", stored=True),
        _ix("rIPSS-WM", core.ripss_wm(age, lab(LOINC_B2M), lab(LOINC_LDH), lab(LOINC_ALBUMIN)),
            "возраст, β2M, ЛДГ, альбумин (анализы на диагнозе)"),
    ]


def _mzl(pt, mm, labs, age):
    malt = mm.get("mzl_subtype") == "malt"
    result = core.malt_ipi(age, _adv(pt), _ldh(mm.get("ldh_uln"))) if malt else None
    return [_ix("MALT-IPI", result, "возраст, стадия, ЛДГ; только для экстранодальной ЛМЗ (MALT)", stored=True)]


def _ptcl(pt, mm, labs, age):
    adv, ldh = _adv(pt), _ldh(mm.get("ldh_uln"))
    nasal = _yes_no(mm.get("nasal_type"))
    pink = (core.pink(age, adv, _yes_no(mm.get("distant_ln")), None if nasal is None else not nasal)
            if mm.get("ptcl_subtype") == "enktl" else None)
    return [
        _ix("IPI", core.ipi(age, pt["ecog"], adv, ldh, _extranodal_gt1(mm.get("extranodal_n"))),
            "возраст, ECOG, стадия Lugano, ЛДГ, число экстранодальных очагов", stored=True),
        _ix("PIT", core.pit(age, pt["ecog"], ldh, _yes_no(mm.get("bm_involvement"))),
            "возраст, ECOG, ЛДГ, поражение костного мозга"),
        _ix("PINK", pink, "только для ENKTL: возраст, стадия, отдалённые лимфоузлы, назальный тип"),
    ]


def _hgbl(pt, mm, labs, age):
    adv, ldh, en = _adv(pt), _ldh(mm.get("ldh_uln")), _extranodal_gt1(mm.get("extranodal_n"))
    return [
        _ix("IPI", core.ipi(age, pt["ecog"], adv, ldh, en),
            "возраст, ECOG, стадия Lugano, ЛДГ, число экстранодальных очагов", stored=True),
        _ix("CNS-IPI", core.cns_ipi(age, pt["ecog"], adv, ldh, en, _yes_no(mm.get("kidney_adrenal"))),
            "факторы IPI и поражение почек / надпочечников"),
    ]


def _pmbcl(pt, mm, labs, age):
    return [_ix("aaIPI", core.aa_ipi(pt["ecog"], _adv(pt), _ldh(mm.get("ldh_uln"))),
                "стадия Lugano, ЛДГ, ECOG", stored=True)]


def _burkitt(pt, mm, labs, age):
    return [_ix("BL-IPI", core.bl_ipi(age, pt["ecog"], _ldh(mm.get("ldh_uln")), _yes_no(mm.get("cns_involvement"))),
                "возраст, ECOG, ЛДГ (> 3 × ВГН), поражение ЦНС", stored=True)]


def _pcnsl(pt, mm, labs, age):
    return [
        _ix("IELSG", core.ielsg(age, pt["ecog"], _ldh(mm.get("ldh_uln")), _yes_no(mm.get("csf_protein")),
                                _yes_no(mm.get("deep_brain"))),
            "возраст, ECOG, ЛДГ, белок ликвора, глубокие структуры мозга", stored=True),
        _ix("MSKCC", core.mskcc_pcnsl(age, _gt(mm.get("kps"), "ge_70", "lt_70")), "возраст, индекс Карновского"),
    ]


# нозология -> (функция расчёта, названия индексов в порядке вывода; первый — индекс карточки)
CALCULATORS = {
    "cll_sll": (_cll, ("CLL-IPI",)),
    "dlbcl": (_dlbcl, ("IPI", "R-IPI", "NCCN-IPI")),
    "follicular": (_fl, ("FLIPI", "FLIPI-2", "PRIMA-PI")),
    "mcl": (_mcl, ("sMIPI", "MIPI-c")),
    "hodgkin": (_hodgkin, ("IPS", "GHSG")),
    "waldenstrom": (_wm, ("IPSS-WM", "rIPSS-WM")),
    "mzl": (_mzl, ("MALT-IPI",)),
    "ptcl": (_ptcl, ("IPI", "PIT", "PINK")),
    "hgbl": (_hgbl, ("IPI", "CNS-IPI")),
    "pmbcl": (_pmbcl, ("aaIPI",)),
    "burkitt": (_burkitt, ("BL-IPI",)),
    "pcnsl": (_pcnsl, ("IELSG", "MSKCC")),
}


def computed_indices(pt: dict, mm: dict[str, str], labs: list[dict]) -> list[dict]:
    """[{name, stored, result, needs}]: stored — индекс вносится и в карточку (сверяется);
    result — None, если какого-то компонента нет."""
    calc = CALCULATORS.get(pt["who_haem5"])
    return calc[0](pt, mm, labs, _age(pt["birth_date"], pt["dx_date"])) if calc else []


# ---------------------------------------------------------------------------
# Предупреждения и сведения модулей
# ---------------------------------------------------------------------------
def module_quality(pt: dict, mm: dict[str, str]) -> list[str]:
    """Предупреждения «проверьте диагноз»: диагноз не меняем, только просим проверить.
    ДВКЛ: MYC + BCL2 — по WHO-HAEM5 это DLBCL/HGBL-MYC/BCL2; ФЛ: грейд 3B (ICC)."""
    if pt["who_haem5"] == "dlbcl" and mm.get("myc_r") == "detected" and mm.get("bcl2_r") == "detected":
        return ["dlbcl_myc_bcl2"]
    if pt["who_haem5"] == "follicular" and mm.get("grade_icc") == "3B":
        return ["fl_grade_3b"]
    if pt["who_haem5"] == "hgbl" and mm.get("hgbl_subtype") == "myc_bcl2" and not (
            mm.get("myc_r") == "detected" and mm.get("bcl2_r") == "detected"):
        return ["hgbl_myc_bcl2_unconfirmed"]
    if pt["who_haem5"] == "burkitt" and mm.get("myc_ig") == "not_detected":
        return ["burkitt_no_myc"]
    return []


def pod24_status(pt: dict, lines: list[dict], observations: list[dict]) -> dict:
    """POD24 для ФЛ (registry_core.pod24) с режимами иммунохимиотерапии из узла дерева."""
    node = core.find_node(core.NOSOLOGY_TREE, pt["who_haem5"]) or {}
    return core.pod24(pt, lines, observations, node.get("pod24_regimens", []))


def _fl_info(pt, mm, lines, observations, markers):
    p = pod24_status(pt, lines, observations)
    text = {"pod24": f"да — прогрессия через {p.get('months')} мес. от начала 1-й линии",
            "no_pod24": "нет — без прогрессии 24 мес. от начала 1-й линии",
            "not_applicable": "не применимо — 1-я линия не иммунохимиотерапия или не начата",
            }.get(p["status"]) or f"пока не определено — {p.get('reason')}"
    info = [{"title": "POD24", "text": text, "warn": p["status"] == "pod24"}]
    tr = [m for m in markers if m["field_key"] == "transformation" and m["value"] == "yes"]
    if tr:
        when = tr[-1]["test_date"]
        info.append({"title": "Трансформация в ДВКЛ", "warn": True,
                     "text": f"да{f', биопсия {when:%d.%m.%Y}' if when else ''} — исходы после неё анализируйте отдельно"})
    return info


def _mcl_info(pt, mm, lines, observations, markers):
    risks = [name for name, on in (("TP53", _detected(mm.get("tp53_mcl"))),
                                   ("бластоидный/плеоморфный вариант", _yes_no(mm.get("blastoid")))) if on]
    if not risks:
        return []
    return [{"title": "Высокий биологический риск", "warn": True,
             "text": ", ".join(risks) + " — учитывайте при выборе терапии (плохой ответ на иммунохимиотерапию)"}]


def _mzl_info(pt, mm, lines, observations, markers):
    info = []
    if mm.get("mzl_subtype") == "malt" and mm.get("malt_site") == "stomach" and mm.get("hp") == "detected":
        info.append({"title": "H. pylori+", "warn": False,
                     "text": "желудочная MALT с H. pylori — эрадикация как первый этап, контроль эндоскопически"})
    if mm.get("hcv") == "detected":
        info.append({"title": "HCV+", "warn": False, "text": "рассмотрите противовирусную терапию HCV"})
    return info


def _hgbl_info(pt, mm, lines, observations, markers):
    info = []
    if mm.get("cns_involvement") == "yes":
        info.append({"title": "Поражение ЦНС", "warn": True, "text": "на диагнозе — режим с проникновением в ЦНС"})
    cns = next(i for i in _hgbl(pt, mm, [], _age(pt["birth_date"], pt["dx_date"])) if i["name"] == "CNS-IPI")
    if cns["result"] and cns["result"]["group"] == "high":
        info.append({"title": "Высокий риск рецидива в ЦНС", "warn": True,
                     "text": f"CNS-IPI {cns['result']['score']} — рассмотрите профилактику ЦНС"})
    return info


def _burkitt_info(pt, mm, lines, observations, markers):
    info = []
    if mm.get("hiv") == "detected":
        info.append({"title": "ВИЧ+", "warn": True, "text": "ВИЧ-ассоциированная ЛБ — АРВТ вместе с химиотерапией"})
    if mm.get("cns_involvement") == "yes":
        info.append({"title": "Поражение ЦНС", "warn": True, "text": "режим с высокими дозами метотрексата и цитарабина"})
    return info


INFO = {"follicular": _fl_info, "mcl": _mcl_info, "mzl": _mzl_info, "hgbl": _hgbl_info, "burkitt": _burkitt_info}


def module_info(pt: dict, mm: dict[str, str], lines: list[dict], observations: list[dict],
                markers: list[dict]) -> list[dict]:
    """Сведения модуля для карточки: [{title, text, warn}]."""
    fn = INFO.get(pt["who_haem5"])
    return fn(pt, mm, lines, observations, markers) if fn else []

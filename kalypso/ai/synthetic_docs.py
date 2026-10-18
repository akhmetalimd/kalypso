"""Синтетические заключения с известными правильными ответами (валидационный набор).

Сырой текст нарочно содержит вымышленные идентификаторы (ФИО, ИИН, телефон,
номера документов): так проверяется и деидентификация. Формулировки варьируются,
часть требует интерпретации (процент идентичности IGHV, Deauville словами),
часть — с отрицаниями. Шаблоны проще реальных заключений: точность на этом
наборе — верхняя оценка, а не точность на реальных данных.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

from kalypso.synthetic import synthetic_iin

SURNAMES_M = ["Ахметов", "Иванов", "Сейткали", "Нурланов", "Петров", "Ким", "Жумабаев", "Смирнов"]
SURNAMES_F = ["Ахметова", "Иванова", "Нурланова", "Петрова", "Жумабаева", "Смирнова", "Ким", "Оспанова"]
NAMES_M = ["Ерлан", "Сергей", "Асхат", "Олег", "Данияр", "Виктор"]
NAMES_F = ["Айгуль", "Елена", "Гульнара", "Ольга", "Динара", "Наталья"]
PATR_M = ["Серикович", "Иванович", "Болатович", "Петрович", "Ерланұлы"]
PATR_F = ["Сериковна", "Ивановна", "Болатовна", "Петровна", "Ерланқызы"]


@dataclass
class SyntheticDoc:
    doc_type: str
    raw_text: str
    truth: dict[str, str]
    doc_date: date


def _fio(rng: random.Random, sex: str, short: bool | None = None) -> str:
    s, n, pt = ((rng.choice(SURNAMES_M), rng.choice(NAMES_M), rng.choice(PATR_M)) if sex == "M"
                else (rng.choice(SURNAMES_F), rng.choice(NAMES_F), rng.choice(PATR_F)))
    short = rng.random() < 0.4 if short is None else short
    return f"{s} {n[0]}.{pt[0]}." if short else f"{s} {n} {pt}"


def _phone(rng) -> str:
    return f"+7 7{rng.randint(0, 99):02d} {rng.randint(100, 999)} {rng.randint(10, 99)} {rng.randint(10, 99)}"


def _header(rng, sex, birth):
    who = "Пациент" if sex == "M" else "Пациентка"
    parts = [f"{who}: {_fio(rng, sex)}"]
    if rng.random() < 0.6:
        parts.append(f"{birth:%d.%m.%Y} г.р.")
    if rng.random() < 0.6:
        parts.append(f"ИИН {synthetic_iin(rng, birth, sex)}")
    line = ", ".join(parts) + "."
    if rng.random() < 0.5:
        line += f" История болезни № {rng.randint(2019, 2025)}/{rng.randint(100, 9999)}."
    if rng.random() < 0.3:
        line += f" Тел. {_phone(rng)}."
    return line


def _histology(rng, sex, birth):
    truth = {}
    variant = rng.choice(["cll", "cll", "sll"])
    truth["diagnosis"] = variant
    material = ("трепанобиоптат костного мозга" if variant == "cll" and rng.random() < 0.5
                else "биоптат шейного лимфатического узла")
    dx = {"cll": rng.choice(["Хронический лимфоцитарный лейкоз (ХЛЛ).",
                             "Морфологическая картина и иммунофенотип соответствуют ХЛЛ."]),
          "sll": rng.choice(["Лимфома из малых лимфоцитов (ЛМЛ).",
                             "Лимфоцитарная лимфома из малых лимфоцитов, признаков лейкемизации нет."])}[variant]
    text = (f"{_header(rng, sex, birth)}\nМатериал: {material}. Блок № {rng.randint(2024, 2025)}-{rng.randint(1000, 9999)}.\n"
            f"Микроскопия: диффузная инфильтрация мелкими лимфоцитами"
            f"{' с пролиферативными центрами' if rng.random() < 0.6 else ''}.\n"
            f"ИГХ: CD5+, CD23+, CD20 слабо+, CD10-, cyclin D1-, LEF1+. Ki-67 {rng.randint(5, 30)}%.\n"
            f"Заключение: {dx}\nВрач-патологоанатом: {_fio(rng, rng.choice('MF'), short=True)}")
    return text, truth


_DETECTED = ["выявлена в {p}% ядер", "обнаружена ({p}% ядер)", "положительный результат, {p}% ядер"]
_NOT_DETECTED = ["не выявлена", "не обнаружена (0% ядер)", "отрицательный результат"]


def _fish_result(rng, positive_rate):
    if rng.random() < positive_rate:
        return "detected", rng.choice(_DETECTED).format(p=rng.randint(8, 80))
    return "not_detected", rng.choice(_NOT_DETECTED)


def _cytogenetics(rng, sex, birth):
    truth, parts = {}, []
    names = {"del17p": "del(17p)", "del11q": "del(11q)", "tri12": "трисомия 12", "del13q": "del(13q)"}
    for key, rate in (("del17p", 0.1), ("del11q", 0.15), ("tri12", 0.15), ("del13q", 0.5)):
        if rng.random() < 0.9:       # иногда зонд не исследовался
            truth[key], res = _fish_result(rng, rate)
            parts.append(f"{names[key]}: {res}")
    text = (f"{_header(rng, sex, birth)}\nЦитогенетическое исследование (FISH), материал: периферическая кровь.\n"
            f"Зонды: TP53 (17p13), ATM (11q22), CEP12, D13S319 (13q14).\nРезультат: " + ";\n".join(parts) + ".\n"
            f"Исследование провела: {_fio(rng, 'F', short=True)}")
    return text, truth


def _molecular(rng, sex, birth):
    truth, lines = {}, []
    if rng.random() < 0.9:
        identity = round(rng.uniform(88, 100), 1)
        truth["IGHV"] = "mutated" if identity < 98 else "unmutated"
        gene = f"IGHV{rng.choice(['1-69', '3-23', '4-34', '3-21'])}"
        style = rng.randint(0, 2)
        if style == 0:
            status = "мутированный" if identity < 98 else "немутированный"
            lines.append(f"IGHV: перестройка {gene}, идентичность с зародышевой линией "
                         f"{str(identity).replace('.', ',')}% — {status} статус.")
        elif style == 1:   # только процент — нужна интерпретация (< 98 % = мутированный)
            lines.append(f"IGHV: {gene}, идентичность {str(identity).replace('.', ',')}%.")
        else:
            lines.append(f"Мутационный статус IGHV: {'мутированный' if identity < 98 else 'немутированный'} "
                         f"({str(identity).replace('.', ',')}% гомологии).")
    if rng.random() < 0.9:
        if rng.random() < 0.15:
            truth["TP53"] = "mutated"
            lines.append(f"TP53: выявлена мутация c.{rng.randint(500, 900)}G>A (p.Arg248Gln), VAF {rng.randint(5, 60)}%.")
        else:
            truth["TP53"] = "wild_type"
            lines.append(rng.choice(["TP53: мутации не обнаружены (NGS, чувствительность 5%).",
                                     "Мутаций в гене TP53 не выявлено."]))
    text = (f"{_header(rng, sex, birth)}\nМолекулярно-генетическое исследование (секвенирование).\n"
            + "\n".join(lines) + f"\nАмб. карта № {rng.randint(10000, 99999)}.")
    return text, truth


def _pet_ct(rng, sex, birth):
    truth = {}
    response = rng.choice(["CR", "PR", "SD", "PD"])
    truth["response"] = response
    phrase = {"CR": "Полный метаболический ответ.", "PR": "Частичный метаболический ответ.",
              "SD": "Стабилизация процесса, без признаков прогрессирования.",
              "PD": "Прогрессирование заболевания: новые очаги."}[response]
    ds = {"CR": rng.choice([1, 2, 3]), "PR": 4, "SD": rng.choice([4, 5]), "PD": 5}[response]
    ds_text = ""
    if rng.random() < 0.85:
        truth["deauville"] = str(ds)
        ds_text = rng.choice([f" Deauville {ds}.", f" По шкале Довиль — {ds} балла.", f" DS {ds}."])
    text = (f"{_header(rng, sex, birth)}\nПЭТ-КТ всего тела с 18F-ФДГ, сравнение с исследованием от "
            f"{rng.randint(1, 28):02d}.{rng.randint(1, 12):02d}.2024.\n"
            f"Лимфоузлы шеи и подмышечных областей до {rng.randint(10, 35)} мм, SUVmax {rng.uniform(2, 12):.1f}.\n"
            f"Заключение: {phrase}{ds_text}")
    return text, truth


_MAKERS = {"histology": _histology, "cytogenetics": _cytogenetics, "molecular": _molecular, "pet_ct": _pet_ct}


def generate(n: int, seed: int = 1, doc_types: tuple[str, ...] = tuple(_MAKERS)) -> list[SyntheticDoc]:
    rng = random.Random(seed)
    docs = []
    for i in range(n):
        doc_type = doc_types[i % len(doc_types)]
        sex = rng.choice("MF")
        birth = date(rng.randint(1940, 1975), rng.randint(1, 12), rng.randint(1, 28))
        text, truth = _MAKERS[doc_type](rng, sex, birth)
        docs.append(SyntheticDoc(doc_type, text, truth, date(rng.randint(2019, 2026), rng.randint(1, 9), rng.randint(1, 28))))
    return docs

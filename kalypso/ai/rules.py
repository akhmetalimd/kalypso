"""Извлечение на правилах (без сети): базовая линия для валидации и режим без AI.

Отрицания проверяются раньше утверждений («del(17p) не выявлена» -> not_detected).
Уверенность правил — не выше medium: формулировки в реальных заключениях
разнообразнее шаблонов.
"""
from __future__ import annotations

import re

from kalypso.ai.base import Extraction, Extractor, Finding
from kalypso.ai.fields import targets_for

_I = re.IGNORECASE
_NEG = r"(?:не\s+выявлен\w*|не\s+обнаружен\w*|отсутству\w*|отрицательн\w*|negative|не\s+определя\w*|(?<![\d,.])0\s*%)"
_POS = r"(?:выявлен\w*|обнаружен\w*|положительн\w*|positive|определя\w*|\d{1,2}(?:[.,]\d+)?\s*%\s*(?:ядер|клеток))"
_GAP = r"[^.\n;]{0,70}?"

_PROBES = {
    "del17p": r"(?:del\s*\(?\s*17p\s*\)?|17p13|делеци\w*\s+17p)",
    "del11q": r"(?:del\s*\(?\s*11q\s*\)?|11q22|делеци\w*\s+11q)",
    "tri12": r"(?:трисоми\w*\s+(?:хромосомы\s+)?12|\+\s?12\b|trisomy\s+12|CEP\s?12)",
    "del13q": r"(?:del\s*\(?\s*13q\s*\)?|13q14|D13S319|делеци\w*\s+13q)",
}

# target -> [(шаблон, значение | функция от match, уверенность)]
_RULES: dict[str, list] = {
    "diagnosis": [
        (r"(?:\bХЛЛ\b|хроническ\w*\s+лимфоцитарн\w*\s+лейкоз\w*|\bCLL\b)", "cll", "medium"),
        (r"(?:лимфом\w*\s+из\s+малых\s+лимфоцитов|лимфоцитарн\w*\s+лимфом\w*|\bЛМЛ\b|\bSLL\b)", "sll", "medium"),
    ],
    "IGHV": [
        (r"IGHV" + _GAP + r"(?:немутирован\w*|unmutated|без\s+соматическ\w*\s+(?:гипер)?мутаци\w*)", "unmutated", "medium"),
        (r"IGHV" + _GAP + r"(?:мутирован\w*|mutated|соматическ\w*\s+гипермутаци\w*)", "mutated", "medium"),
        (r"IGHV" + _GAP + r"(\d{2,3}(?:[.,]\d+)?)\s*%\s*(?:гомолог\w*|идентичн\w*|identity)",
         lambda m: "mutated" if float(m.group(1).replace(",", ".")) < 98 else "unmutated", "medium"),
    ],
    "TP53": [
        (r"мутаци\w*" + r"[^.\n;]{0,30}?TP53" + r"[^.\n;]{0,30}?не\s+(?:выявлен|обнаружен)\w*", "wild_type", "medium"),
        (r"TP53" + _GAP + r"(?:мутаци\w*\s+не\s+(?:выявлен|обнаружен)\w*|не\s+выявлен\w*|не\s+обнаружен\w*|"
         r"дикий\s+тип|wild[- ]?type)", "wild_type", "medium"),
        (r"TP53" + _GAP + r"(?:мутаци\w*\s+[cp]\.|[cp]\.\d+|VAF|выявлен\w*\s+мутаци\w*|мутаци\w*\s+выявлен\w*)", "mutated", "medium"),
    ],
    **{key: [
        (probe + _GAP + _NEG, "not_detected", "medium"),
        (probe + _GAP + _POS, "detected", "medium"),
    ] for key, probe in _PROBES.items()},
    "deauville": [
        (r"(?:Deauville|Довиль\w*|\bDS)\s*[:\-–—]?\s*(?:балл\w*\s*)?([1-5])\b", lambda m: m.group(1), "medium"),
    ],
    "response": [
        (r"полн\w*\s+(?:метаболическ\w*\s+)?(?:ремисси\w*|ответ\w*)", "CR", "low"),
        (r"частичн\w*\s+(?:метаболическ\w*\s+)?(?:ремисси\w*|ответ\w*)", "PR", "low"),
        (r"стабилизац\w*", "SD", "low"),
        (r"без\s+признаков\s+прогрессирован\w*", None, None),   # стоп: это не PD
        (r"прогрессирован\w*|прогрессия\s+заболевания", "PD", "low"),
    ],
}
_COMPILED = {t: [(re.compile(rx, _I), v, c) for rx, v, c in rules] for t, rules in _RULES.items()}


class RuleExtractor(Extractor):
    name = "rules"
    prompt_version = "rules-1"

    def extract(self, doc_type: str, text: str) -> Extraction:
        findings = []
        for target in targets_for(doc_type):
            for rx, value, confidence in _COMPILED.get(target, []):
                m = rx.search(text)
                if not m:
                    continue
                if value is None:       # стоп-шаблон: правило не применяем
                    break
                findings.append(Finding(target, value(m) if callable(value) else value, m.group(0), confidence))
                break
        return Extraction(findings, self.name, self.prompt_version)

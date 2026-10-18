"""SVG-графики: валидный XML, нужное число меток, классы вместо цветов, экранирование."""
import xml.etree.ElementTree as ET
from datetime import date

from kalypso import charts
from registry_core import km_estimate

NS = "{http://www.w3.org/2000/svg}"


def parse(svg):
    root = ET.fromstring(svg)
    assert root.find(f"{NS}title").text and root.get("role") == "img"
    return root


def by_class(root, cls):
    return [e for e in root.iter() if cls in (e.get("class") or "").split()]


def test_km_chart_groups_and_censoring():
    groups = [{"label": f"Г{i}", "km": km_estimate([30, 60, 90, 120], [1, 0, 1, 0])} for i in range(3)]
    root = parse(charts.km_chart(groups, "OS"))
    assert len([e for e in by_class(root, "line") if e.tag == f"{NS}path"]) == 3
    assert len(by_class(root, "tick")) == 3 * 2               # по 2 цензурирования в группе
    assert by_class(root, "band") == []                         # ДИ-полосы только для 1–2 групп
    assert "fill=" not in charts.km_chart(groups, "OS")         # цвет — классами, не атрибутами


def test_km_chart_caps_series():
    groups = [{"label": str(i), "km": km_estimate([10], [1])} for i in range(9)]
    root = parse(charts.km_chart(groups, "x"))
    assert len([e for e in by_class(root, "line") if e.tag == f"{NS}path"]) == charts.MAX_SERIES


def test_km_ci_band_for_single_group():
    root = parse(charts.km_chart([{"label": "Все", "km": km_estimate([10, 20, 30], [1, 1, 0])}], "x"))
    assert len(by_class(root, "band")) == 1


def test_bar_chart_muted_and_escaping():
    svg = charts.bar_chart([("<script>", 3, False), ("Не выполнялось", 2, True)], "IGHV")
    root = parse(svg)
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert len(by_class(root, "muted-fill")) == 1 and len(by_class(root, "s1")) == 1
    assert "3 · 60 %" in svg


def test_column_stacked_timeline_parse():
    parse(charts.column_chart([("2020", 3), ("2021", 0)], "Годы"))
    root = parse(charts.stacked_bars([("Линия 1", {"CR": 2, "PR": 1})], [("CR", "CR"), ("PR", "PR")], "Ответ"))
    assert len(by_class(root, "seg")) == 2 + 2                  # 2 сегмента + 2 образца в легенде
    parse(charts.timeline(date(2020, 1, 1), date(2024, 1, 1),
                          [{"label": "1: BR", "short": "1: BR", "start": date(2020, 3, 1), "end": None}],
                          [{"date": date(2022, 1, 1), "kind": "death", "label": "Смерть", "short": "смерть"}],
                          [{"date": date(2020, 9, 1), "label": "CR", "short": "CR"}], "Таймлайн"))

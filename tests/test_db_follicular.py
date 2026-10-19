"""Миграция 0007 (модуль ФЛ): индекс FLIPI в карточке; стадия Lugano — общая с ДВКЛ (0005)."""
import pytest

from conftest import rejected

FL = dict(who_haem5="follicular", icd_o_3_2="9690/3", mkb="C82.1")


def test_flipi_accepted(reg):
    reg.patient("990000000701", **FL, stage_system="Lugano", stage_value="IV",
                index_name="FLIPI", index_score=3, index_group="high")
    reg.con.commit()


@pytest.mark.parametrize("kw, match", [
    ({"index_name": "FLIPI", "index_score": 6, "index_group": "high"}, "index_flipi"),
    ({"index_name": "FLIPI", "index_score": 2, "index_group": "low_intermediate"}, "index_flipi"),
    ({"stage_system": "Lugano", "stage_value": "A"}, "stage_lugano"),
])
def test_flipi_constraints(reg, kw, match):
    with rejected(reg.con, match):
        reg.patient("990000000702", **FL, **kw)

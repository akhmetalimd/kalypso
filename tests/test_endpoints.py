"""Конечные точки, Каплан–Майер с ДИ, иерархия Döhner (registry_core, без БД)."""
from datetime import date

import pytest

from registry_core import at_risk_table, dohner_group, endpoint, impute_date, km_estimate

PT = {"dx_date": date(2020, 1, 1), "dx_date_precision": "day",
      "last_contact_date": date(2023, 6, 1), "last_contact_precision": "day"}
LINES = [{"line_no": 1, "start_date": date(2021, 1, 1), "start_precision": "day"},
         {"line_no": 2, "start_date": date(2022, 3, 1), "start_precision": "day"}]
OBS = [{"event_type": "progression", "event_date": date(2022, 1, 1), "obs_date": date(2022, 1, 1), "obs_precision": "day"},
       {"event_type": "new_line_start", "event_date": date(2022, 3, 1), "obs_date": date(2022, 3, 1), "obs_precision": "day"},
       {"event_type": "death", "event_date": date(2023, 6, 1), "obs_date": date(2023, 6, 1), "obs_precision": "day"}]


@pytest.mark.parametrize("name, line_no, end, event, reason", [
    ("OS", 1, date(2023, 6, 1), 1, "death"),
    ("TTFT", 1, date(2021, 1, 1), 1, "first_line"),
    ("PFS", 1, date(2022, 1, 1), 1, "progression"),
    ("mPFS", 1, date(2022, 1, 1), 1, "progression"),
    ("EFS", 1, date(2022, 1, 1), 1, "progression"),
    ("TTNT", 1, date(2022, 3, 1), 1, "next_line"),
    ("PFS", 2, date(2023, 6, 1), 1, "death"),       # прогрессия до старта 2-й линии не считается
    ("EFS", 2, date(2023, 6, 1), 1, "death"),       # new_line_start в день старта линии — не исход
    ("TTNT", 2, date(2023, 6, 1), 1, "death"),
])
def test_endpoints_hand_computed(name, line_no, end, event, reason):
    e = endpoint(name, PT, LINES, OBS, line_no)
    assert (e["end"], e["event"], e["reason"]) == (end, event, reason)
    assert e["days"] == (end - e["start"]).days


def test_line_endpoint_not_applicable_without_line():
    assert endpoint("PFS", PT, LINES, OBS, 3) is None


def test_censoring_at_last_contact():
    e = endpoint("OS", PT, [], [])
    assert (e["end"], e["event"], e["reason"]) == (date(2023, 6, 1), 0, "censored")


def test_ttft_death_without_treatment_is_censored():
    dead = [{"event_type": "death", "event_date": date(2023, 6, 1), "obs_date": date(2023, 6, 1), "obs_precision": "day"}]
    e = endpoint("TTFT", PT, [], dead)
    assert (e["event"], e["end"]) == (0, date(2023, 6, 1))


def test_mpfs_counts_next_line_before_progression():
    obs = [{"event_type": "progression", "event_date": date(2022, 6, 1), "obs_date": date(2022, 6, 1), "obs_precision": "day"}]
    e = endpoint("mPFS", PT, LINES, obs, 1)
    assert (e["end"], e["reason"]) == (date(2022, 3, 1), "next_line")
    assert endpoint("PFS", PT, LINES, obs, 1)["reason"] == "progression"


def test_imprecise_dates_imputed_mid_period():
    assert impute_date(date(2021, 3, 1), "month") == date(2021, 3, 15)
    assert impute_date(date(2021, 3, 1), "year") == date(2021, 7, 1)
    pt = {**PT, "dx_date": date(2020, 5, 1), "dx_date_precision": "year"}
    assert endpoint("OS", pt, [], [])["start"] == date(2020, 7, 1)


def test_contradictory_dates_excluded():
    obs = [{"event_type": "death", "event_date": date(2019, 1, 1), "obs_date": date(2019, 1, 1), "obs_precision": "day"}]
    assert endpoint("OS", PT, [], obs) == {"excluded": "окончание раньше начала отсчёта"}


def test_unknown_endpoint():
    with pytest.raises(ValueError):
        endpoint("DFS", PT, [], [])


# --- Каплан–Майер: классический набор Freireich (6-MP), опубликованные оценки ------
SIX_MP = "6 6 6 6+ 7 9+ 10 10+ 11+ 13 16 17+ 19+ 20+ 22 23 25+ 32+ 32+ 34+ 35+".split()
D = [int(x.rstrip("+")) for x in SIX_MP]
E = [0 if x.endswith("+") else 1 for x in SIX_MP]


def test_km_matches_published_6mp():
    km = km_estimate(D, E)
    got = {s["t"]: (round(s["survival"], 3), round(s["se"], 3)) for s in km["steps"]}
    assert got == {6: (0.857, 0.076), 7: (0.807, 0.087), 10: (0.753, 0.096), 13: (0.690, 0.107),
                   16: (0.627, 0.114), 22: (0.538, 0.128), 23: (0.448, 0.135)}
    first = km["steps"][0]
    assert (round(first["ci_low"], 3), round(first["ci_high"], 3)) == (0.620, 0.952)   # log-log
    assert km["median"] == 23 and km["median_ci"] == (13, None)
    assert (km["n"], km["events"], len(km["censored"])) == (21, 9, 12)


def test_at_risk_table():
    assert at_risk_table(D, [0, 10, 20, 30]) == [21, 15, 8, 4]


@pytest.mark.parametrize("markers, group", [
    ({"del17p": "detected"}, "del17p"),
    ({"del17p": "not_detected", "del11q": "detected"}, "del11q"),
    ({"del17p": "not_detected", "del11q": "not_detected", "tri12": "detected"}, "tri12"),
    ({"del17p": "not_detected", "del11q": "not_detected", "tri12": "not_detected", "del13q": "detected"}, "del13q_sole"),
    ({"del17p": "not_detected", "del11q": "normal_negative", "tri12": "not_detected", "del13q": "not_detected"}, "normal"),
    ({"del17p": "not_done", "del11q": "detected"}, "unknown"),        # вышестоящая проба не исследована
    ({}, "unknown"),
])
def test_dohner_hierarchy(markers, group):
    assert dohner_group(markers) == group

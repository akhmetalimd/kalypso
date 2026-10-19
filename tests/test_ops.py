"""Эксплуатация: маска ИИН в журналах, /healthz, check, seed-synthetic, настройки, упаковка."""
import logging
import logging.config
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo

from conftest import REPO
from kalypso import cli, logsafe, ops, registry
from kalypso.config import ConfigError, Settings, load_settings
from kalypso.web.app import create_app

IIN = "610412412345"
SECRET = "s" * 32


# --- журналы без ИИН ----------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    (f"duplicate key: Key (iin)=({IIN}) already exists", "duplicate key: Key (iin)=(***) already exists"),
    ("блок 2024-1234567, 13 цифр 6104124123456", "блок 2024-1234567, 13 цифр 6104124123456"),
    (f"{IIN},{IIN}", "***,***"),
])
def test_mask(text, expected):
    assert logsafe.mask(text) == expected


def _emit(record_args, msg="%s", exc=None) -> str:
    """Запись через обработчик с фильтром — как в log_config()."""
    import io
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(logsafe.MaskIIN())
    log = logging.getLogger("kalypso.test_mask")
    log.addHandler(handler)
    log.propagate = False
    try:
        if exc:
            try:
                raise exc
            except Exception:
                log.exception(msg, *record_args)
        else:
            log.error(msg, *record_args)
    finally:
        log.removeHandler(handler)
    return stream.getvalue()


def test_filter_masks_args_and_traceback():
    out = _emit((f"ИИН {IIN}",), exc=ValueError(f"Key (iin)=({IIN})"))
    assert IIN not in out and "***" in out and "Traceback" in out


def test_filter_keeps_access_log_args_tuple():
    from uvicorn.logging import AccessFormatter
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("10.0.0.1:5000", "GET", f"/patients?q={IIN}", "1.1", 200), None)
    assert logsafe.MaskIIN().filter(record)
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False).format(record)
    assert IIN not in line and "/patients?q=***" in line


def test_log_config_filters_every_handler():
    cfg = logsafe.log_config()
    assert cfg["handlers"] and all("mask_iin" in h["filters"] for h in cfg["handlers"].values())
    logging.config.dictConfig(cfg)                     # конфигурация применяется без ошибок
    for name in ("uvicorn", "uvicorn.access", "kalypso"):     # uvicorn.error пишет через "uvicorn"
        handlers = logging.getLogger(name).handlers
        assert handlers and all(any(isinstance(f, logsafe.MaskIIN) for f in h.filters) for h in handlers), name


# --- /healthz ----------------------------------------------------------------
def test_healthz(world):
    app = create_app(Settings(dsn=world.dsn, session_secret=SECRET.encode(), secure_cookies=False), world.db)
    r = TestClient(app).get("/healthz")
    assert (r.status_code, r.text) == (200, "ok") and r.headers["Cache-Control"] == "no-store"


def test_healthz_without_db():
    class Broken:
        def tx(self, *a, **kw):
            raise OSError("connection refused to 10.0.0.5")
    app = create_app(Settings(dsn="postgresql://x", session_secret=SECRET.encode()), Broken())
    r = TestClient(app).get("/healthz")
    assert r.status_code == 503 and "10.0.0.5" not in r.text


# --- настройки ---------------------------------------------------------------
@pytest.mark.parametrize("env, message", [
    ({}, "KALYPSO_DSN"),
    ({"KALYPSO_DSN": "postgresql://x"}, "KALYPSO_SESSION_SECRET"),
    ({"KALYPSO_DSN": "postgresql://x", "KALYPSO_SESSION_SECRET": "short"}, "32"),
])
def test_settings_errors(env, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(env)


def test_settings_secure_by_default():
    s = load_settings({"KALYPSO_DSN": "postgresql://x", "KALYPSO_SESSION_SECRET": SECRET})
    assert s.secure_cookies is True and s.session_hours == 8 and SECRET not in repr(s)


# --- check -------------------------------------------------------------------
@pytest.fixture
def web_login(world, pg):
    """Логин приложения как в развёртывании: член kalypso_app, не суперпользователь, не владелец."""
    import uuid
    name = f"t_web_{uuid.uuid4().hex[:8]}"
    with pg.admin() as con:
        con.execute(f"CREATE ROLE {name} LOGIN IN ROLE kalypso_app")
    yield make_conninfo(world.dsn, user=name)
    with pg.admin() as con:
        con.execute(f"DROP ROLE {name}")


def test_check_passes_for_app_login(web_login):
    assert ops.run_checks(Settings(dsn=web_login, session_secret=SECRET.encode())) == []


def test_check_reports_problems(world):
    problems = ops.run_checks(Settings(dsn=world.dsn, session_secret=SECRET.encode(), secure_cookies=False))
    text = " ".join(problems)
    assert "суперпользователь" in text and "KALYPSO_SECURE_COOKIES=0" in text
    unreachable = ops.run_checks(Settings(dsn="postgresql://nobody@127.0.0.1:1/x", session_secret=SECRET.encode()))
    assert any("Нет подключения" in p for p in unreachable)


def test_check_command_exit_codes(web_login, capsys):
    assert cli.main(["check"], env={"KALYPSO_DSN": web_login, "KALYPSO_SESSION_SECRET": SECRET}) == 0
    assert "Готово к запуску" in capsys.readouterr().out
    assert cli.main(["check"], env={"KALYPSO_DSN": web_login}) == 1
    assert "KALYPSO_SESSION_SECRET" in capsys.readouterr().err


def test_check_detects_missing_migration(web_login, world, monkeypatch):
    from kalypso import migrate
    real = migrate.migration_files
    monkeypatch.setattr(migrate, "migration_files", lambda: real() + [("0099_future", Path("0099_future.sql"))])
    assert ops.run_checks(Settings(dsn=web_login, session_secret=SECRET.encode())) == [
        "Не применены миграции: 0099_future"]


# --- seed-synthetic ------------------------------------------------------------
def test_seed_synthetic_only_into_empty_registry(world, capsys):
    summary = ops.seed_synthetic(world.db, cll=6, dlbcl=4)
    assert summary["cll_sll"]["patients"] == 6 and summary["dlbcl"]["patients"] == 4
    patients = registry.list_patients(world.db, world.curator)
    assert len(patients) == 10 and {p["who_haem5"] for p in patients} == {"cll_sll", "dlbcl"}
    with pytest.raises(ops.OpsError, match="пустой регистр"):
        ops.seed_synthetic(world.db, cll=1, dlbcl=0)
    assert cli.main(["seed-synthetic", "--cll", "1"], env={"KALYPSO_DSN": world.dsn}) == 1
    out = capsys.readouterr()
    assert "пустой регистр" in out.err
    assert not re.search(r"\d{12}", out.out + out.err)              # ИИН в консоль не печатаются


def test_seed_needs_physicians_and_curator(world):
    import psycopg
    with psycopg.connect(world.dsn) as con:              # служебно, в обход приложения
        con.execute("UPDATE app_user SET is_active = false WHERE role_code = 'curator'")
    with pytest.raises(ops.OpsError, match="куратор"):
        ops.seed_synthetic(world.db, cll=1, dlbcl=0)


# --- упаковка ------------------------------------------------------------------
def test_packaging_matches_sources():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    for module in project["tool"]["setuptools"]["py-modules"]:
        assert (REPO / f"{module}.py").exists(), module
    req = [l.strip() for l in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
           if l.strip() and not l.startswith("#")]
    assert sorted(req) == sorted(project["project"]["dependencies"])


def test_image_excludes_dev_credentials_and_secrets():
    dockerfile = (REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert "rm -f kalypso/dev_seed.json" in dockerfile and "USER kalypso" in dockerfile
    ignore = (REPO / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", ".devdb", "tests", "*.dump", "*.key"} <= set(ignore)
    env_example = (REPO / "deploy" / ".env.example").read_text(encoding="utf-8")
    assert re.findall(r"^[A-Z_]+=(.*)$", env_example, re.M) == ["", "", "", "", "8"]   # секретов в образце нет


# --- serve -------------------------------------------------------------------
def test_serve_passes_proxy_and_log_settings(monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    env = {"KALYPSO_DSN": "postgresql://kalypso_web@db/kalypso", "KALYPSO_SESSION_SECRET": SECRET,
           "KALYPSO_HOST": "0.0.0.0", "KALYPSO_FORWARDED_ALLOW_IPS": "*"}
    monkeypatch.setattr("kalypso.web.app.Database", lambda dsn: object())   # без подключения к БД
    assert cli.main(["serve"], env=env) == 0
    assert (seen["host"], seen["port"], seen["proxy_headers"], seen["forwarded_allow_ips"]) == ("0.0.0.0", 8000, True, "*")
    assert "mask_iin" in seen["log_config"]["filters"] and seen["server_header"] is False
    seen.clear()
    assert cli.main(["serve"], env={k: v for k, v in env.items() if k not in ("KALYPSO_HOST", "KALYPSO_FORWARDED_ALLOW_IPS")}) == 0
    assert (seen["host"], seen["forwarded_allow_ips"]) == ("127.0.0.1", "127.0.0.1")    # по умолчанию — только локально

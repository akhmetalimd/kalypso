"""dev-запуск: повтор старта встроенного PostgreSQL после аварийной остановки (kalypso.dev._start_postgres).

pgserver кладёт объект сервера в кэш ещё до проверки запуска и потом возвращает тот же объект —
без сброса кэша повторы не помогали бы, даже когда база уже поднялась."""
import subprocess
from types import SimpleNamespace

import pytest

from kalypso import dev


class _FakeServer:
    def __init__(self, ready: bool):
        self.ready = ready

    def get_uri(self, database=None):
        assert self.ready                   # как в pgserver: _postmaster_info ещё нет
        return "postgresql://fake"


def _fake_pgserver(fail_first: int, error=AssertionError):
    """Как pgserver: объект попадает в кэш до проверки запуска; первые fail_first запусков неудачны."""
    cache, calls = {}, []

    class PostgresServer:
        _instances = cache

    def get_server(pgdata, cleanup_mode=None):
        calls.append(pgdata)
        if pgdata in cache:
            return cache[pgdata]
        ok = len(calls) > fail_first
        srv = _FakeServer(ok)
        cache[pgdata] = srv                 # сначала в кэш, потом проверка
        if not ok and error is subprocess.TimeoutExpired:
            raise subprocess.TimeoutExpired("pg_ctl", 10)
        return srv

    return SimpleNamespace(get_server=get_server, postgres_server=SimpleNamespace(PostgresServer=PostgresServer)), calls


@pytest.mark.parametrize("error", [AssertionError, subprocess.TimeoutExpired])
def test_retry_succeeds_once_server_is_ready(error, capsys):
    pg, calls = _fake_pgserver(fail_first=3, error=error)
    srv = dev._start_postgres(pg, attempts=6, pause=0)
    assert srv.get_uri() == "postgresql://fake" and len(calls) == 4
    assert capsys.readouterr().out.count("повторная попытка") == 3


def test_gives_up_after_attempts():
    pg, calls = _fake_pgserver(fail_first=10)
    with pytest.raises(AssertionError):
        dev._start_postgres(pg, attempts=3, pause=0)
    assert len(calls) == 3 and pg.postgres_server.PostgresServer._instances == {}


def test_forget_server_tolerates_other_pgserver_layout():
    dev._forget_server(SimpleNamespace(), "x")    # нет postgres_server — без ошибки

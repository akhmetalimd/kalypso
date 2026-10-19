"""Веб-интерфейс: вход, пациенты (ХЛЛ, ДВКЛ, ФЛ), карточка, аналитика, администрирование, аудит.

Серверный рендеринг (Jinja2), без JavaScript. ИИН передаётся только в теле POST
(не в URL — URL попадают в журналы доступа). В интерфейсе ИИН видят только роли
с правом patient.iin (врач своего филиала, куратор).
"""
from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import registry_core as core
from kalypso import admin, analytics, auth, charts, docgen, export, i18n, investigations, registry
from kalypso.config import Settings
from kalypso.db import Database
from kalypso.rbac import AccessDenied, NotFound, Principal
from kalypso.registry import ValidationError

HERE = Path(__file__).resolve().parent
COOKIE = "kalypso_session"
LOGIN_COOKIE = "kalypso_login"


class LoginRequired(Exception):
    pass


class DemoEnter(Exception):
    """Демо-режим: сессии нет — входим демо-пользователем и возвращаемся на ту же страницу."""
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


class PasswordChangeRequired(Exception):
    pass


def _fmt_date(value, precision: str = "day") -> str:
    if not value:
        return "—"
    if precision == "year":
        return f"{value.year}"
    if precision == "month":
        return f"{value.month:02d}.{value.year}"
    return value.strftime("%d.%m.%Y")


def _short(value) -> str:
    return str(value)[:8] if value else "—"


SPECIAL_DASHBOARDS = {"cll_sll": "/dashboard/cll", "dlbcl": "/dashboard/dlbcl", "follicular": "/dashboard/fl"}


def dashboard_url(nosology: str) -> str:
    return SPECIAL_DASHBOARDS.get(nosology, f"/dashboard/m/{nosology}")


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    db = db or Database(settings.dsn)
    app = FastAPI(title="KALYPSO", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(label=i18n.label, options=i18n.options, flag=i18n.flag,
                       lab_units=core.LAB_UNITS, core_fields=registry.core_fields,
                       active_nodes=registry.active_nodes, missing_codes=registry.MISSING,
                       conditional_fields=registry.CONDITIONAL_FIELDS,
                       profile=registry.profile, indexes=core.INDEXES, dashboard_url=dashboard_url,
                       nosology_groups=registry.nosology_groups, inv=investigations)
    env.filters.update(d=_fmt_date, short=_short,
                       nosology=lambda node_id: (core.find_node(core.NOSOLOGY_TREE, node_id or "") or {}).get("name", node_id))

    # --- инфраструктура -----------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    def current(request: Request, allow_pw_change: bool = False) -> Principal:
        p = auth.principal_for_token(db, request.cookies.get(COOKIE))
        if p is None:
            if settings.demo_mode:
                raise DemoEnter(request.url.path if request.method == "GET" else "/")
            raise LoginRequired()
        if p.must_change_password and not allow_pw_change:
            raise PasswordChangeRequired()
        return p

    async def form_of(request: Request) -> dict:
        form = dict(await request.form())
        if not auth.csrf_valid(settings.session_secret, request.cookies.get(COOKIE), form.pop("csrf", None)):
            raise AccessDenied("csrf")
        return form

    def render(request: Request, name: str, p: Principal | None, status_code=200, **ctx) -> HTMLResponse:
        token = request.cookies.get(COOKIE)
        ctx.update(p=p, csrf=auth.csrf_token(settings.session_secret, token) if token else "",
                   demo_mode=settings.demo_mode, demo_users=auth.demo_users(db) if settings.demo_mode and p else [],
                   current_path=request.url.path,
                   message=i18n.msg(request.query_params.get("m")),
                   warnings=[w for w in request.query_params.get("w", "").split(",") if w])
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back(url: str, m: str | None = None, warnings: list[str] | None = None) -> RedirectResponse:
        q = {}
        if m:
            q["m"] = m
        if warnings:
            q["w"] = ",".join(warnings)
        return RedirectResponse(url + ("?" + urlencode(q) if q else ""), status_code=303)

    @app.exception_handler(LoginRequired)
    async def _login_required(request, exc):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(DemoEnter)
    async def _demo_enter(request, exc):
        return RedirectResponse("/demo/enter?" + urlencode({"next": exc.path}), status_code=303)

    def _demo_cookie(resp, token: str):
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=settings.secure_cookies,
                        max_age=settings.session_hours * 3600)
        return resp

    def _safe_next(path: str | None) -> str:
        return path if path and path.startswith("/") and not path.startswith("//") else "/"

    @app.get("/demo/enter")
    def demo_enter(request: Request, next: str = "/"):
        """Демо-режим: вход первым врачом без пароля (затем роль меняется в шапке)."""
        if not settings.demo_mode:
            raise NotFound()
        users = auth.demo_users(db)
        if not users:
            return render(request, "error.html", None, 503, title="Нет пользователей",
                          text="Демо-режим: в БД нет активных пользователей.")
        token, _ = auth.demo_session(db, users[0].login, session_hours=settings.session_hours)
        return _demo_cookie(RedirectResponse(_safe_next(next), status_code=303), token)

    @app.post("/demo/role")
    async def demo_role(request: Request):
        """Демо-режим: сменить пользователя (роль). Прежняя сессия закрывается."""
        if not settings.demo_mode:
            raise NotFound()
        p = current(request)
        form = await form_of(request)
        try:
            token, _ = auth.demo_session(db, form.get("login", ""), session_hours=settings.session_hours)
        except auth.LoginFailed:
            raise NotFound() from None
        auth.logout(db, request.cookies[COOKIE], p)
        return _demo_cookie(RedirectResponse(_safe_next(form.get("next")), status_code=303), token)

    @app.exception_handler(PasswordChangeRequired)
    async def _pw_required(request, exc):
        return RedirectResponse("/password", status_code=303)

    @app.exception_handler(NotFound)
    async def _not_found(request, exc):
        return render(request, "error.html", None, 404, title="Не найдено",
                      text="Запись не найдена или недоступна вашей учётной записи.")

    @app.exception_handler(AccessDenied)
    async def _denied(request, exc):
        text = ("Форма устарела — обновите страницу и повторите." if str(exc) == "csrf"
                else "У вашей роли нет доступа к этому разделу.")
        return render(request, "error.html", None, 403, title="Доступ запрещён", text=text)

    def pid_of(value: str) -> UUID:
        try:
            return UUID(value)
        except ValueError:
            raise NotFound() from None

    # --- вход ---------------------------------------------------------------
    # CSRF на форме входа (до появления сессии): случайный токен в cookie
    # (SameSite=Strict, только /login) + его HMAC в скрытом поле формы.
    def _login_page(request: Request, status_code=200, errors=None, login=""):
        token = secrets.token_urlsafe(24)
        resp = render(request, "login.html", None, status_code, errors=errors, login=login,
                      login_csrf=auth.csrf_token(settings.session_secret, "login:" + token))
        resp.set_cookie(LOGIN_COOKIE, token, httponly=True, samesite="strict", path="/login",
                        secure=settings.secure_cookies, max_age=1800)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if settings.demo_mode:
            return RedirectResponse("/", status_code=303)
        return _login_page(request)

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        token = request.cookies.get(LOGIN_COOKIE)
        if not auth.csrf_valid(settings.session_secret, "login:" + token if token else None, form.get("csrf")):
            return _login_page(request, 403, ["Форма входа устарела — повторите вход."], form.get("login", ""))
        try:
            token, p = auth.login(db, form.get("login", ""), form.get("password", ""),
                                  session_hours=settings.session_hours,
                                  ip=request.client.host if request.client else None,
                                  user_agent=request.headers.get("user-agent"))
        except auth.LoginFailed as e:
            text = ("Слишком много неудачных попыток. Попробуйте через 15 минут."
                    if e.reason == "locked" else "Неверный логин или пароль.")
            return _login_page(request, 401, [text], form.get("login", ""))
        resp = RedirectResponse("/password" if p.must_change_password else "/", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=settings.secure_cookies,
                        max_age=settings.session_hours * 3600)
        resp.delete_cookie(LOGIN_COOKIE, path="/login")
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        p = current(request, allow_pw_change=True)
        await form_of(request)
        auth.logout(db, request.cookies[COOKIE], p)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/password", response_class=HTMLResponse)
    def password_page(request: Request):
        return render(request, "password.html", current(request, allow_pw_change=True))

    @app.post("/password")
    async def password_submit(request: Request):
        p = current(request, allow_pw_change=True)
        form = await form_of(request)
        errors = []
        if form.get("new") != form.get("new2"):
            errors.append("Новые пароли не совпадают")
        else:
            try:
                auth.change_password(db, p, form.get("old", ""), form.get("new", ""),
                                     keep_token=request.cookies.get(COOKIE))
            except ValueError as e:
                errors.append(str(e))
        if errors:
            return render(request, "password.html", p, 422, errors=errors)
        return back("/patients", "password_changed")

    # --- пациенты -----------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        """Проверка живости для Docker и мониторинга: без входа, без данных, только SELECT 1."""
        try:
            with db.tx() as cur:
                cur.execute("SELECT 1")
        except Exception:  # noqa: BLE001 — причина в ответ не попадает
            return Response("db unavailable", status_code=503, media_type="text/plain")
        return Response("ok", media_type="text/plain")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        """Главная: название платформы и расшифровка аббревиатуры."""
        return render(request, "home.html", current(request))

    PAGE = 100

    def _patient_page(request: Request, p, f: dict) -> dict:
        """Список пациентов по фильтрам, постранично; query — фильтры для ссылок страниц."""
        rows = registry.list_patients(db, p, filters=f)
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            page = 1
        pages = max(1, -(-len(rows) // PAGE))
        page = min(page, pages)
        query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "page"])
        return {"rows": rows[(page - 1) * PAGE: page * PAGE], "total": len(rows), "page": page,
                "pages": pages, "query": query}

    @app.get("/patients", response_class=HTMLResponse)
    def patients(request: Request):
        p = current(request)
        f = analytics.parse_filters(dict(request.query_params))
        return render(request, "patients.html", p, f=f, pl=_patient_page(request, p, f),
                      branches=admin.list_branches(db, p))

    @app.post("/patients/search")
    async def patients_search(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            pid = registry.find_by_iin(db, p, form.get("iin", ""))
        except ValidationError as e:
            f = analytics.parse_filters({})
            return render(request, "patients.html", p, 422, errors=e.errors, f=f, pl=_patient_page(request, p, f),
                          branches=admin.list_branches(db, p))
        if pid is None:
            return back("/patients", "not_found_iin")
        return back(f"/patients/{pid}")

    @app.get("/patients/new", response_class=HTMLResponse)
    def patient_new(request: Request, who: str | None = None):
        """Сначала нозология (от неё зависят стадия, индекс и поля), затем форма — без JS."""
        p = current(request)
        p.require("patient.write")
        nodes = registry.active_nodes()
        if who not in {n["id"] for n in nodes}:
            if len(nodes) > 1:
                return render(request, "nosology_choose.html", p, nodes=nodes)
            who = nodes[0]["id"]
        return render(request, "patient_form.html", p, v={}, missing={}, is_new=True, who=who)

    @app.post("/patients/new")
    async def patient_create(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            res = registry.register_patient(db, p, form)
        except ValidationError as e:
            form.pop("iin", None)  # ИИН в ответ не возвращаем — пользователь вводит заново
            return render(request, "patient_form.html", p, 422, errors=e.errors, v=form,
                          missing=_missing_from_form(form), is_new=True)
        if res.status == "exists_requested":
            return back("/patients", "exists_requested")
        return back(f"/patients/{res.record_id}", res.status, res.warnings)

    @app.get("/patients/{pid}", response_class=HTMLResponse)
    def patient_card(request: Request, pid: str):
        p = current(request)
        card = registry.get_card(db, p, pid_of(pid))
        return render(request, "patient_card.html", p, c=card, pt=card["patient"], timeline_svg=_timeline(card))

    @app.get("/patients/{pid}/edit", response_class=HTMLResponse)
    def patient_edit(request: Request, pid: str):
        p = current(request)
        p.require("patient.write")
        card = registry.get_card(db, p, pid_of(pid))
        return render(request, "patient_form.html", p, v=card["patient"], missing=card["missing"],
                      is_new=False, pid=pid, has_death=any(o["event_type"] == "death" for o in card["observations"]))

    @app.post("/patients/{pid}/edit")
    async def patient_update(request: Request, pid: str):
        p = current(request)
        form = await form_of(request)
        try:
            res = registry.update_patient(db, p, pid_of(pid), form)
        except ValidationError as e:
            return render(request, "patient_form.html", p, 422, errors=e.errors, v=form,
                          missing=_missing_from_form(form), is_new=False, pid=pid)
        return back(f"/patients/{pid}", "saved", res.warnings)

    def _timeline(card: dict) -> str | None:
        pt = card["patient"]
        if not card["lines"] and not card["observations"]:
            return None
        imp = lambda d, prec="day": core.impute_date(d, prec)  # noqa: E731
        lines = [{"label": f"Линия {l['line_no']}: {l['regimen']}", "short": f"{l['line_no']}: {l['regimen'][:18]}",
                  "start": imp(l["start_date"], l["start_precision"]),
                  "end": imp(l["end_date"], l["end_precision"]) if l["end_date"] else None} for l in card["lines"]]
        events = [{"date": o["event_date"], "kind": o["event_type"], "label": f"{i18n.label('event_type', o['event_type'])} "
                   f"{o['event_date']:%d.%m.%Y}", "short": i18n.label("event_type", o["event_type"]).lower()[:10]}
                  for o in card["observations"] if o["event_type"] != "none" and o["event_date"]]
        responses = [{"date": o["obs_date"], "label": f"Ответ {o['response_value']} ({o['obs_date']:%d.%m.%Y})",
                      "short": o["response_value"]} for o in card["observations"] if o["response_value"]]
        start = imp(pt["dx_date"], pt["dx_date_precision"])
        end = max([imp(pt["last_contact_date"], pt["last_contact_precision"])]
                  + [l["end"] or l["start"] for l in lines] + [e["date"] for e in events] + [r["date"] for r in responses])
        return charts.timeline(start, end, lines, events, responses, "Таймлайн пациента")

    def _record_route(path: str, fn):
        @app.post(f"/patients/{{pid}}/{path}", name=f"add_{path}")
        async def handler(request: Request, pid: str):
            p = current(request)
            form = await form_of(request)
            try:
                res = fn(db, p, pid_of(pid), form)
            except ValidationError as e:
                card = registry.get_card(db, p, pid_of(pid))
                return render(request, "patient_card.html", p, 422, c=card, pt=card["patient"],
                              errors=e.errors, open_form=path, v=form)
            return back(f"/patients/{pid}", "saved", res.warnings)

    _record_route("lines", registry.add_line)
    _record_route("observations", registry.add_observation)
    _record_route("labs", registry.add_lab)
    _record_route("markers", registry.add_marker)
    _record_route("investigations", registry.add_investigation)

    editors = {"line": registry.update_line, "observation": registry.update_observation,
               "lab": registry.update_lab, "marker": registry.update_marker,
               "investigation": registry.update_investigation}

    def _record_form(request, p, pid, kind, rid, v, status_code=200, errors=None):
        card = registry.get_card(db, p, pid_of(pid))
        rec = registry.get_record(db, p, pid_of(pid), kind, pid_of(rid))
        return render(request, "record_form.html", p, status_code, errors=errors, kind=kind, pid=pid, rid=rid,
                      v=v, pt=card["patient"], lines=card["lines"], field_key=rec.get("field_key"),
                      inv_kind=rec.get("kind"))

    @app.get("/patients/{pid}/edit/{kind}/{rid}", response_class=HTMLResponse)
    def record_edit(request: Request, pid: str, kind: str, rid: str):
        p = current(request)
        if kind not in editors:
            raise NotFound()
        rec = dict(registry.get_record(db, p, pid_of(pid), kind, pid_of(rid)))
        if kind == "lab":  # в форме — исходное значение и единица, как вводил врач
            rec.update(value=rec["value_raw"], unit=rec["unit_raw"])
        if kind == "investigation":  # маркеры ИФТ — строкой, как в заключении
            rec["markers"] = investigations.format_markers(rec["markers"])
        return _record_form(request, p, pid, kind, rid, rec)

    @app.post("/patients/{pid}/edit/{kind}/{rid}")
    async def record_update(request: Request, pid: str, kind: str, rid: str):
        p = current(request)
        if kind not in editors:
            raise NotFound()
        form = await form_of(request)
        try:
            res = editors[kind](db, p, pid_of(pid), pid_of(rid), form)
        except ValidationError as e:
            return _record_form(request, p, pid, kind, rid, form, 422, e.errors)
        return back(f"/patients/{pid}", "saved", res.warnings)

    @app.post("/patients/{pid}/delete/{kind}/{rid}")
    async def record_delete(request: Request, pid: str, kind: str, rid: str):
        p = current(request)
        await form_of(request)
        try:
            registry.soft_delete(db, p, pid_of(pid), kind, pid_of(rid))
        except ValidationError as e:
            card = registry.get_card(db, p, pid_of(pid))
            return render(request, "patient_card.html", p, 422, c=card, pt=card["patient"], errors=e.errors)
        return back(f"/patients/{pid}", "deleted")

    # --- П3: дашборды и «Исследователь выживаемости» --------------------------
    def _filters_text(f: dict) -> str:
        parts = [f"филиал {f['branch']}" if f["branch"] else "все доступные филиалы"]
        if f.get("nosology"):
            parts.append(registry.profile(f["nosology"])["name"])
        if f["year_from"] or f["year_to"]:
            parts.append(f"диагноз {f['year_from'] or '…'}–{f['year_to'] or '…'}")
        if f["sex"]:
            parts.append(i18n.label("sex", f["sex"]).lower())
        if f.get("stage"):
            parts.append("стадия " + f["stage"].replace(":", " "))
        if f.get("status"):
            parts.append(i18n.label("status_last_contact", f["status"]).lower())
        if f["provenance"]:
            parts.append(i18n.label("provenance", f["provenance"]).lower())
        return ", ".join(parts)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        p = current(request)
        f = analytics.parse_filters(dict(request.query_params))
        d = analytics.overview(db, p, f)
        return render(request, "dashboard.html", p, d=d, f=f, branches=admin.list_branches(db, p),
                      charts=charts, filters_text=_filters_text(f))

    @app.get("/dashboard/cll", response_class=HTMLResponse)
    def dashboard_cll(request: Request):
        p = current(request)
        f = analytics.parse_filters({**request.query_params, "nosology": "cll_sll"})
        d = analytics.cll_dashboard(db, p, f)
        return render(request, "dashboard_cll.html", p, d=d, f=f, branches=admin.list_branches(db, p),
                      pl=_patient_page(request, p, f),
                      charts=charts, filters_text=_filters_text(f), response_groups=analytics.RESPONSE_GROUPS["cll_sll"])

    @app.get("/dashboard/dlbcl", response_class=HTMLResponse)
    def dashboard_dlbcl(request: Request):
        p = current(request)
        f = analytics.parse_filters({**request.query_params, "nosology": "dlbcl"})
        d = analytics.dlbcl_dashboard(db, p, f)
        return render(request, "dashboard_dlbcl.html", p, d=d, f=f, branches=admin.list_branches(db, p),
                      pl=_patient_page(request, p, f),
                      charts=charts, filters_text=_filters_text(f), response_groups=analytics.RESPONSE_GROUPS["dlbcl"])

    @app.get("/dashboard/m/{nosology}", response_class=HTMLResponse)
    def dashboard_module(request: Request, nosology: str):
        p = current(request)
        if nosology in SPECIAL_DASHBOARDS:
            return RedirectResponse(SPECIAL_DASHBOARDS[nosology], status_code=303)
        if nosology not in {n["id"] for n in registry.active_nodes()}:
            raise NotFound()
        f = analytics.parse_filters({**request.query_params, "nosology": nosology})
        d = analytics.module_dashboard(db, p, nosology, f)
        return render(request, "dashboard_module.html", p, d=d, f=f, branches=admin.list_branches(db, p),
                      pl=_patient_page(request, p, f),
                      charts=charts, filters_text=_filters_text({**f, "nosology": nosology}),
                      response_groups=analytics.response_groups(nosology))

    @app.get("/dashboard/fl", response_class=HTMLResponse)
    def dashboard_fl(request: Request):
        p = current(request)
        f = analytics.parse_filters({**request.query_params, "nosology": "follicular"})
        d = analytics.fl_dashboard(db, p, f)
        return render(request, "dashboard_fl.html", p, d=d, f=f, branches=admin.list_branches(db, p),
                      pl=_patient_page(request, p, f),
                      charts=charts, filters_text=_filters_text(f),
                      response_groups=analytics.RESPONSE_GROUPS["follicular"])

    def _explore(request: Request, p):
        q = dict(request.query_params)
        f = analytics.parse_filters(q)
        try:
            line_no = max(1, min(int(q.get("line_no") or 1), 5))
        except ValueError:
            line_no = 1
        try:
            threshold = float(q["threshold"].replace(",", ".")) if q.get("threshold") else None
        except ValueError:
            threshold = None
        loinc = q.get("lab") if q.get("lab") in core.LAB_UNITS and not q.get("lab", "_").startswith("_") else None
        res = analytics.explore(db, p, q.get("endpoint") or "OS", line_no, q.get("grouping") or "none",
                                f, loinc, threshold)
        return res, f, q

    @app.get("/survival", response_class=HTMLResponse)
    def survival(request: Request):
        p = current(request)
        res, f, q = _explore(request, p)
        grouping_label = analytics.GROUPINGS[res["grouping"]]
        f = {**f, "nosology": res["nosology"]}   # группировка могла сузить когорту до своей нозологии
        abstract = docgen.cohort_abstract(res, _filters_text(f), grouping_label) if res["groups"] else ""
        return render(request, "survival.html", p, r=res, f=f, q=q, branches=admin.list_branches(db, p),
                      charts=charts, endpoints=core.ENDPOINTS, line_endpoints=core.LINE_ENDPOINTS,
                      groupings=analytics.GROUPINGS, grouping_sets=analytics.GROUPING_SETS,
                      abstract=abstract, grouping_label=grouping_label, csv_query=str(request.query_params))

    @app.get("/survival.csv")
    def survival_csv(request: Request):
        p = current(request)
        res, _, _ = _explore(request, p)
        return Response("﻿" + analytics.explore_csv(res), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kalypso_survival.csv"'})

    @app.get("/patients/{pid}/docs/{kind}", response_class=HTMLResponse)
    def patient_doc(request: Request, pid: str, kind: str):
        p = current(request)
        if kind not in ("tumor-board", "epicrisis"):
            raise NotFound()
        card = registry.get_card(db, p, pid_of(pid))
        s = docgen.patient_summary(card)
        text = docgen.tumor_board_text(s) if kind == "tumor-board" else docgen.epicrisis_text(s)
        return render(request, "patient_doc.html", p, s=s, text=text, kind=kind, pid=pid)

    # --- контроль полноты (FR-7) -------------------------------------------
    @app.get("/quality", response_class=HTMLResponse)
    def quality(request: Request, flag: str | None = None):
        p = current(request)
        patients = registry.list_patients(db, p)

        def keys(r):  # все «нет ответа по линии N» сводятся в один флаг
            return {"missing_response_line_any" if f.startswith("missing_response_line_") else f for f in r["flags"]}
        counts: dict[str, int] = {}
        for r in patients:
            for k in keys(r):
                counts[k] = counts.get(k, 0) + 1
        selected = [r for r in patients if flag and flag in keys(r)]
        return render(request, "quality.html", p, counts=sorted(counts.items(), key=lambda kv: -kv[1]),
                      total=len(patients), sel_flag=flag, selected=selected)

    # --- куратор: запросы доступа ------------------------------------------
    @app.get("/requests", response_class=HTMLResponse)
    def requests_page(request: Request):
        p = current(request)
        return render(request, "requests.html", p, requests=admin.list_requests(db, p))

    @app.post("/requests/{rid}")
    async def request_decide(request: Request, rid: str):
        p = current(request)
        form = await form_of(request)
        admin.decide_request(db, p, pid_of(rid), form.get("decision") == "approve", form.get("note"))
        return back("/requests", "request_decided")

    # --- администратор ------------------------------------------------------
    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request):
        p = current(request)
        return render(request, "users.html", p, users=admin.list_users(db, p),
                      branches=admin.list_branches(db, p))

    @app.post("/admin/users")
    async def users_create(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            admin.create_user(db, p, form)
        except ValidationError as e:
            form.pop("password", None)
            return render(request, "users.html", p, 422, errors=e.errors, v=form,
                          users=admin.list_users(db, p), branches=admin.list_branches(db, p))
        return back("/admin/users", "user_created")

    @app.post("/admin/users/{uid}/active")
    async def users_active(request: Request, uid: str):
        p = current(request)
        form = await form_of(request)
        try:
            admin.set_active(db, p, pid_of(uid), form.get("active") == "1")
        except ValidationError as e:
            return render(request, "users.html", p, 422, errors=e.errors,
                          users=admin.list_users(db, p), branches=admin.list_branches(db, p))
        return back("/admin/users", "saved")

    @app.post("/admin/users/{uid}/password")
    async def users_password(request: Request, uid: str):
        p = current(request)
        form = await form_of(request)
        try:
            admin.reset_password(db, p, pid_of(uid), form.get("password", ""))
        except ValidationError as e:
            return render(request, "users.html", p, 422, errors=e.errors,
                          users=admin.list_users(db, p), branches=admin.list_branches(db, p))
        return back("/admin/users", "saved")

    @app.post("/admin/branches")
    async def branches_create(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            admin.create_branch(db, p, form)
        except ValidationError as e:
            return render(request, "users.html", p, 422, errors=e.errors,
                          users=admin.list_users(db, p), branches=admin.list_branches(db, p))
        return back("/admin/users", "saved")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, table: str | None = None, row_id: str | None = None):
        p = current(request)
        return render(request, "audit.html", p, rows=admin.list_audit(db, p, table or None, row_id or None),
                      table=table or "", row_id=row_id or "")

    @app.get("/audit/logins", response_class=HTMLResponse)
    def logins_page(request: Request):
        p = current(request)
        return render(request, "logins.html", p, rows=admin.list_logins(db, p))

    # --- аналитик -----------------------------------------------------------
    @app.get("/export", response_class=HTMLResponse)
    def export_page(request: Request):
        p = current(request)
        p.require("export.deid")
        return render(request, "export.html", p)

    @app.get("/export/patients.csv")
    def export_csv(request: Request):
        p = current(request)
        data = export.patients_csv(db, p)
        return Response("﻿" + data, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kalypso_patients_deid.csv"'})

    return app


def _missing_from_form(form: dict) -> dict:
    return {k[:-len("__missing")]: v for k, v in form.items() if k.endswith("__missing") and v}

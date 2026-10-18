"""Веб-интерфейс П1: вход, пациенты ХЛЛ, карточка, администрирование, аудит.

Серверный рендеринг (Jinja2), без JavaScript. ИИН передаётся только в теле POST
(не в URL — URL попадают в журналы доступа) и не возвращается в ответах.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import registry_core as core
from kalypso import admin, auth, export, i18n, registry
from kalypso.config import Settings
from kalypso.db import Database
from kalypso.deid import IinHasher
from kalypso.rbac import AccessDenied, NotFound, Principal
from kalypso.registry import ValidationError

HERE = Path(__file__).resolve().parent
COOKIE = "kalypso_session"


class LoginRequired(Exception):
    pass


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


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    db = db or Database(settings.dsn)
    hasher = IinHasher(settings.hmac_key, settings.hash_key_version)
    app = FastAPI(title="KALYPSO", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.globals.update(label=i18n.label, options=i18n.options, flag=i18n.flag,
                       lab_units=core.LAB_UNITS, core_fields=registry.core_fields,
                       active_nodes=registry.active_nodes, missing_codes=registry.MISSING,
                       conditional_fields=registry.CONDITIONAL_FIELDS,
                       iwcll=registry.IWCLL_RESPONSES)
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
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html", None)

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        try:
            token, p = auth.login(db, form.get("login", ""), form.get("password", ""),
                                  session_hours=settings.session_hours,
                                  ip=request.client.host if request.client else None,
                                  user_agent=request.headers.get("user-agent"))
        except auth.LoginFailed as e:
            text = ("Слишком много неудачных попыток. Попробуйте через 15 минут."
                    if e.reason == "locked" else "Неверный логин или пароль.")
            return render(request, "login.html", None, 401, errors=[text], login=form.get("login", ""))
        resp = RedirectResponse("/password" if p.must_change_password else "/patients", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=settings.secure_cookies,
                        max_age=settings.session_hours * 3600)
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
    @app.get("/")
    def root():
        return RedirectResponse("/patients", status_code=303)

    @app.get("/patients", response_class=HTMLResponse)
    def patients(request: Request):
        p = current(request)
        return render(request, "patients.html", p, patients=registry.list_patients(db, p))

    @app.post("/patients/search")
    async def patients_search(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            pid = registry.find_by_iin(db, hasher, p, form.get("iin", ""))
        except ValidationError as e:
            return render(request, "patients.html", p, 422, errors=e.errors,
                          patients=registry.list_patients(db, p))
        if pid is None:
            return back("/patients", "not_found_iin")
        return back(f"/patients/{pid}")

    @app.get("/patients/new", response_class=HTMLResponse)
    def patient_new(request: Request):
        p = current(request)
        p.require("patient.write")
        return render(request, "patient_form.html", p, v={}, missing={}, is_new=True)

    @app.post("/patients/new")
    async def patient_create(request: Request):
        p = current(request)
        form = await form_of(request)
        try:
            res = registry.register_patient(db, hasher, p, form)
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
        return render(request, "patient_card.html", p, c=card, pt=card["patient"])

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

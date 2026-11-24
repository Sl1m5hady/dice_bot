"""FastAPI entry point for Kubomet."""

from hmac import compare_digest, digest
from html import escape
from os import getenv
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadData, URLSafeTimedSerializer
from pydantic import BaseModel, Field, StrictStr

from chat_store import (
    ENV_PATH,
    MAX_CHAT_INPUT,
    MAX_NAME_LENGTH,
    MAX_PLAIN_TEXT,
    ChatStore,
    QueueFull,
    TargetChanged,
    chat_settings,
    detect_roll,
    parse_target,
)
from dice_parser import FormulaError
from service import make_roll_document


ROOT = Path(__file__).resolve().parent
COOKIE = "dice_session"
ADMIN_COOKIE = "dice_admin"
SESSION_AGE = 7 * 24 * 60 * 60
MAX_BODY = 512 * 1024


class Login(BaseModel):
    code: StrictStr = Field(max_length=1024)


class Submission(BaseModel):
    character_id: StrictStr = Field(max_length=36)
    name: StrictStr = Field(max_length=MAX_NAME_LENGTH)
    text: StrictStr = Field(max_length=MAX_CHAT_INPUT)
    target_id: StrictStr | None = Field(max_length=20)


class TargetSettings(BaseModel):
    target_id: StrictStr | None = Field(max_length=20)


def error_response(message, status):
    return JSONResponse({"error": message}, status_code=status)


def supplied_code(value):
    try:
        return value.strip().encode()
    except UnicodeError:
        return b""


def target_label(target):
    return str(target) if target is not None else None


def create_app(config=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    group_id, path = chat_settings()
    dotenv = dotenv_values(ENV_PATH)

    def setting(key, default=""):
        return getenv(key, dotenv.get(key) or default)

    settings = {
        "WEB_ACCESS_CODE": setting("WEB_ACCESS_CODE").strip(),
        "WEB_ADMIN_CODE": setting("WEB_ADMIN_CODE").strip(),
        "SECRET_KEY": setting("WEB_SESSION_SECRET"),
        "DATABASE": path,
        "GROUP_ID": group_id,
        "SESSION_COOKIE_SECURE": setting("WEB_COOKIE_SECURE") == "1",
    }
    if config:
        settings.update(config)
    code = settings["WEB_ACCESS_CODE"].encode()
    admin_code = settings["WEB_ADMIN_CODE"].encode()
    secret = settings["SECRET_KEY"].encode()
    if not code or not secret:
        raise RuntimeError("Задайте WEB_ACCESS_CODE и WEB_SESSION_SECRET в окружении или .env.")
    if admin_code and compare_digest(code, admin_code):
        raise RuntimeError("WEB_ADMIN_CODE и WEB_ACCESS_CODE должны быть разными.")
    session_stamp = digest(secret, code, "sha256").hex()
    admin_stamp = digest(secret, admin_code, "sha256").hex() if admin_code else None
    signer = URLSafeTimedSerializer(secret, salt="kubomet-session")
    store = ChatStore(settings["DATABASE"], default_target=settings["GROUP_ID"])
    app.state.chat_store = store
    app.mount("/static", StaticFiles(directory=ROOT / "web/static"), name="static")

    def has_cookie(request, cookie, stamp):
        token = request.cookies.get(cookie)
        if not token or stamp is None:
            return False
        try:
            return signer.loads(token, max_age=SESSION_AGE) == stamp
        except BadData:
            return False

    def set_session_cookie(response, name, stamp):
        response.set_cookie(
            name, signer.dumps(stamp), httponly=True, secure=settings["SESSION_COOKIE_SECURE"],
            samesite="strict", max_age=SESSION_AGE,
        )

    def clear_session_cookie(response, name):
        response.delete_cookie(name, httponly=True, secure=settings["SESSION_COOKIE_SECURE"], samesite="strict")

    def session_data(authenticated):
        return {
            "authenticated": authenticated,
            "target_id": target_label(store.active_target()) if authenticated else None,
            "admin_enabled": authenticated and admin_stamp is not None,
        }

    @app.middleware("http")
    async def protect(request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path != "/api/session" and not has_cookie(request, COOKIE, session_stamp):
            response = error_response("Войдите по коду доступа.", 401)
        elif path.startswith("/api/admin/") and path != "/api/admin/session" and not has_cookie(request, ADMIN_COOKIE, admin_stamp):
            response = error_response("Введите код администратора.", 403)
        elif request.method in ("POST", "PUT") and int(request.headers.get("content-length") or 0) > MAX_BODY:
            response = error_response("Запрос слишком большой.", 413)
        else:
            response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return error_response("Проверьте поля сообщения или код доступа.", 400)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/templates/index.html", media_type="text/html")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/session")
    def access_session(request: Request):
        return session_data(has_cookie(request, COOKIE, session_stamp))

    @app.post("/api/session")
    def login(attempt: Login):
        if not compare_digest(supplied_code(attempt.code), code):
            return error_response("Неверный код доступа.", 401)
        response = JSONResponse(session_data(True))
        set_session_cookie(response, COOKIE, session_stamp)
        clear_session_cookie(response, ADMIN_COOKIE)
        return response

    @app.delete("/api/session")
    def logout():
        response = JSONResponse({"authenticated": False})
        clear_session_cookie(response, COOKIE)
        clear_session_cookie(response, ADMIN_COOKIE)
        return response

    @app.post("/api/admin/session")
    def admin_login(attempt: Login):
        if admin_stamp is None:
            return error_response("Вход администратора выключен.", 403)
        if not compare_digest(supplied_code(attempt.code), admin_code):
            return error_response("Неверный код администратора.", 403)
        response = JSONResponse({"target_id": target_label(store.active_target())})
        set_session_cookie(response, ADMIN_COOKIE, admin_stamp)
        return response

    @app.get("/api/admin/chat")
    def admin_chat():
        return {"target_id": target_label(store.active_target())}

    @app.put("/api/admin/chat")
    def set_admin_chat(payload: TargetSettings):
        try:
            target = parse_target(payload.target_id)
        except ValueError as error:
            return error_response(str(error), 400)
        store.set_target(target)
        return {"target_id": target_label(target)}

    @app.get("/api/chat")
    def feed():
        target, messages = store.feed()
        return {"target_id": target_label(target), "messages": messages}

    @app.post("/api/chat", status_code=201)
    def submit_message(payload: Submission):
        name, text = payload.name.strip(), payload.text.strip()
        if not name or not text:
            return error_response("Введите имя персонажа и сообщение.", 400)
        try:
            character_id = str(UUID(payload.character_id))
            name.encode("utf-8")
            text.encode("utf-8")
        except (ValueError, UnicodeError):
            return error_response("Проверьте данные персонажа и сообщение.", 400)
        try:
            roll = detect_roll(text)
        except FormulaError as error:
            return error_response(str(error), 400)
        if roll is None and len(text.encode("utf-16-le")) // 2 > MAX_PLAIN_TEXT:
            return error_response(f"Обычное сообщение может содержать до {MAX_PLAIN_TEXT} символов.", 400)
        try:
            entry_id = store.add_web(character_id, name, text, roll, parse_target(payload.target_id))
        except ValueError as error:
            return error_response(str(error), 400)
        except TargetChanged:
            return error_response("Администратор сменил чат. Проверьте, куда отправляется сообщение.", 409)
        except QueueFull:
            return error_response("Очередь Telegram заполнена. Попробуйте чуть позже.", 503)
        return {"id": entry_id}

    @app.get("/api/chat/{entry_id}/result")
    def download_roll(entry_id: int):
        entry = store.full_message(entry_id)
        if entry is None or entry["rendered"] is None:
            return error_response("Бросок не найден.", 404)
        body = f'{escape(entry["text"])}\n\n{entry["rendered"]}'
        return Response(
            make_roll_document(body).content, media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="roll-{entry_id}.html"'},
        )

    @app.get("/api/chat/{entry_id}/text")
    def message_text(entry_id: int):
        entry = store.full_message(entry_id)
        if entry is None:
            return error_response("Сообщение не найдено.", 404)
        return {"text": entry["text"]}

    return app

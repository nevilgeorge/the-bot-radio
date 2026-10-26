import logging
import re
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from . import view_helpers as h
from .store import Store

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title=config.SITE_NAME, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.globals.update(h=h, config=config)

_store = Store(config.DATA_DIR)


def get_store() -> Store:
    return _store


StoreDep = Annotated[Store, Depends(get_store)]


def render(
    request: Request,
    template: str,
    *,
    active_nav: str | None,
    page_title: str,
    meta_description: str = config.SITE_DESCRIPTION,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    now = datetime.now(config.TZ)
    return templates.TemplateResponse(
        request,
        template,
        {
            "active_nav": active_nav,
            "page_title": page_title,
            "meta_description": meta_description,
            "status": h.status_text(now),
            "today": now.date(),
            **context,
        },
        status_code=status_code,
    )


def not_found(request: Request) -> HTMLResponse:
    return render(request, "404.html", active_nav=None, page_title="Dead air", status_code=404)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return not_found(request)
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@app.get("/", response_class=HTMLResponse)
def today_page(request: Request, store: StoreDep, subscribed: str | None = None):
    day = store.latest_day()
    recent = [d for d in store.list_days() if day is None or d.date != day.date][:4]
    return render(
        request,
        "today.html",
        active_nav="today",
        page_title="Today",
        day=day,
        dj=store.persona(day.dj) if day else None,
        recent=recent,
        personas=store.personas(),
        subscribed=subscribed,
    )


@app.get("/index", response_class=HTMLResponse)
def index_page(request: Request, store: StoreDep, dj: str | None = None, month: str | None = None):
    if month is not None and not MONTH_RE.match(month):
        return not_found(request)
    dj = dj or None
    months = store.months(dj=dj)
    selected_month = month or (months[0] if months else None)
    days = store.list_days(dj=dj, month=selected_month) if selected_month else []
    older = [m for m in months if selected_month and m < selected_month]
    all_days = store.list_days()
    return render(
        request,
        "index.html",
        active_nav="index",
        page_title="The Index",
        personas=store.personas(),
        selected_dj=dj,
        selected_month=selected_month,
        days=days,
        prev_month=older[0] if older else None,
        first_day=all_days[-1] if all_days else None,
    )


@app.get("/index/{date_str}", response_class=HTMLResponse)
def day_page(request: Request, date_str: str, store: StoreDep):
    if not DATE_RE.match(date_str):
        return not_found(request)
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return not_found(request)
    day = store.get_day(d)
    if day is None:
        return not_found(request)
    prev_day, next_day = store.prev_next(d)
    return render(
        request,
        "day.html",
        active_nav="index",
        page_title=f"{day.title} ({day.date.isoformat()})",
        meta_description=day.liner_notes[0] if day.liner_notes else config.SITE_DESCRIPTION,
        day=day,
        dj=store.persona(day.dj),
        prev_day=prev_day,
        next_day=next_day,
    )


@app.get("/inputs", response_class=HTMLResponse)
def inputs_page(request: Request, store: StoreDep):
    day = store.latest_day()
    return render(
        request,
        "inputs.html",
        active_nav="inputs",
        page_title="Inputs",
        descriptions=store.input_descriptions(),
        day=day,
        readings={r.key: r for r in day.inputs} if day else {},
    )


@app.get("/bots", response_class=HTMLResponse)
def bots_page(request: Request, store: StoreDep):
    day = store.latest_day()
    personas = store.personas()
    return render(
        request,
        "bots.html",
        active_nav="bots",
        page_title="Bots",
        day=day,
        today_dj=store.persona(day.dj) if day else None,
        personas=personas,
        stats={p.id: store.resident_stats(p.id) for p in personas},
    )


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    return render(request, "about.html", active_nav="about", page_title="About")


@app.post("/subscribe")
def subscribe(email: Annotated[str, Form()] = ""):
    email = email.strip()
    if not EMAIL_RE.match(email):
        return RedirectResponse("/?subscribed=invalid#subscribe", status_code=303)
    log.info("newsletter signup: %s", email)
    return RedirectResponse("/?subscribed=1#subscribe", status_code=303)


@app.get("/healthz")
def healthz(store: StoreDep):
    day = store.latest_day()
    return {"ok": True, "latest_day": day.date.isoformat() if day else None}

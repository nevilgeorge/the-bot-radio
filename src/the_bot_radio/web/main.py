"""Website factory owning an optional refreshed S3 snapshot and its SDK client."""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from the_bot_radio.generation.config import (
    GenerationStorageConfig,
    S3SDKClient,
    create_s3_client,
)
from the_bot_radio.generation.s3_listing import S3DailyListing
from the_bot_radio.generation.s3_storage import S3DailyRecordStore
from the_bot_radio.generation.storage import run_in_thread

from . import config
from . import view_helpers as h
from .daily_cache import S3DailyCache
from .store import Store

log = logging.getLogger(__name__)

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

router = APIRouter()

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
templates.env.globals.update(h=h, config=config)


def get_store(request: Request) -> Store:
    """Resolve the lifespan-owned store, allowing dependency overrides in tests."""
    return request.app.state.store


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


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return not_found(request)
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@router.get("/", response_class=HTMLResponse)
def today_page(request: Request, store: StoreDep):
    day = store.latest_day()
    recent = [d for d in store.list_days() if day is None or d.date != day.date][:4]
    personas = store.personas()
    return render(
        request,
        "today.html",
        active_nav="today",
        page_title="Today",
        day=day,
        dj=store.persona(day.dj) if day else None,
        recent=recent,
        personas=personas,
        stats={p.id: store.resident_stats(p.id) for p in personas},
    )


@router.get("/index", response_class=HTMLResponse)
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


@router.get("/index/{date_str}", response_class=HTMLResponse)
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


@router.get("/inputs", response_class=HTMLResponse)
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


@router.get("/bots", response_class=HTMLResponse)
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


@router.get("/about", response_class=HTMLResponse)
def about_page(request: Request, store: StoreDep):
    days = store.list_days()
    station_log = [
        ("Format", "One playlist a day"),
        ("Drops at", config.GENERATION_TIME_LABEL),
        ("Listening to", "Greenpoint, 11222"),
        ("Residents", f"{len(store.personas())} bots"),
        ("Inputs read", f"{len(store.input_descriptions())} readings"),
        ("Per playlist", f"{len(days[0].tracks)} tracks" if days else "—"),
        ("On air since", h.month_day_year(days[-1].date) if days else "—"),
    ]
    return render(
        request, "about.html", active_nav="about", page_title="About", station_log=station_log
    )


@router.get("/healthz")
def healthz(store: StoreDep):
    day = store.latest_day()
    return {"ok": True, "latest_day": day.date.isoformat() if day else None}


async def refresh_daily_cache(cache: S3DailyCache, interval: float) -> None:
    """Refresh serially, waiting after each attempt and reporting recovery."""
    failed = False
    while True:
        await asyncio.sleep(interval)
        try:
            await cache.refresh()
        except Exception:
            failed = True
            log.exception("S3 daily refresh failed; retaining the previous snapshot")
        else:
            if failed:
                log.info("S3 daily refresh recovered")
            failed = False


def create_app(
    storage_config: GenerationStorageConfig | None = None,
    *,
    client_factory: Callable[[], S3SDKClient] = create_s3_client,
    refresh_interval: float = 60,
) -> FastAPI:
    """Resolve configuration at startup and own all background work and SDK resources."""
    if refresh_interval <= 0:
        raise ValueError("refresh interval must be positive")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configuration = storage_config or GenerationStorageConfig.from_environment(
            Path(os.environ.get("BOT_RADIO_DATA_DIR", config.REPO_ROOT / "data")), os.environ
        )
        if configuration.bucket is None:
            application.state.store = Store(configuration.data_dir)
            yield
            return

        clients: list[S3SDKClient] = []
        refresh_task: asyncio.Task[None] | None = None
        primary_error: BaseException | None = None

        def capture_client() -> None:
            """Retain ownership if startup is cancelled during SDK construction."""
            clients.append(client_factory())

        try:
            await run_in_thread(capture_client)
            client = clients[0]
            cache = S3DailyCache(
                S3DailyListing(client, configuration.bucket, configuration.prefix),
                S3DailyRecordStore(client, configuration.bucket, configuration.prefix),
            )
            await cache.refresh()
            application.state.daily_cache = cache
            application.state.store = Store(configuration.data_dir, daily_source=cache)
            refresh_task = asyncio.create_task(refresh_daily_cache(cache, refresh_interval))
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with suppress(asyncio.CancelledError):
                    await refresh_task
            if clients:
                try:
                    await run_in_thread(clients[0].close)
                except BaseException as error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(f"S3 client cleanup failed: {type(error).__name__}")

    application = FastAPI(title=config.SITE_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.include_router(router)
    return application


app = create_app()

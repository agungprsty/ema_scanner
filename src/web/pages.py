"""Public HTML pages: server-rendered Jinja shells with their titles and Open Graph tags; each page's
data comes from the JSON API through its ES module in src/static/js/pages/. The signal page also renders
its outcome banner and facts on the server, so a shared link reads correctly without JavaScript.

Templates are autoescaped and pages carry no inline script, so they run under the strict CSP of
nginx.conf.example (script-src 'self').
"""

import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.config.settings import (
    COST_MODEL_VERSION,
    GIT_SHA,
    LATE_SIGNAL_GRACE_SECONDS,
    PUBLIC_BASE_URL,
    ROUND_TRIP_FEE_PERCENT,
    SLIPPAGE_BPS_BY_SYMBOL,
    SLIPPAGE_BPS_DEFAULT,
    STOP_SLIPPAGE_MULTIPLIER,
    STRATEGY_KEY,
    STRATEGY_VERSION,
)
from src.services.db import get_signal_by_id, get_strategy_versions
from src.services.ledger import get_signal_candle_window, get_signal_events
from src.services.performance import MIN_BREAKDOWN_SAMPLE, MIN_SAMPLE, MIN_SAMPLE_FOR_TOTALS
from src.web import views

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["wib"] = views.format_wib
templates.env.filters["signed_r"] = views.signed_r
templates.env.filters["link_parts"] = views.link_parts

SITE_NAME = "Crossline"
SITE_DESCRIPTION = (
    "Crossline publishes every signal of fixed MACD crossover rules on Binance USDⓈ-M futures, with each "
    "result booked in R after costs. Research: no validated edge. Not investment advice."
)
# Cache-busting token for the stylesheet and page scripts
ASSET_VERSION = (GIT_SHA or "dev")[:12]
NAV = (("overview", "/", "Overview"), ("signals", "/signals", "Signals"), ("track-record", "/track-record",
       "Track record"), ("status", "/status", "Status"))

# What each version's rules did on historical data, shown before any live statistic. The headline
# figure is under cost model cm1, the same costs as the live KPIs next to it; cheaper cost bases are
# secondary lines, labelled. Sources: the harness runs logged in research/trials.jsonl and the reports
# in docs/research
M = views.MINUS
BASELINES = {
    "macd_cross@2.2.0": {
        "title": "the live rules",
        "rows": [
            {
                "data": "Backtest, Nov 2024 to Sep 2026, 12 symbols",
                "n": "7,450",
                "net": f"{M}0.085R",
                "t": f"{M}6.15",
                "ci": f"[{M}0.126R, {M}0.042R]",
                "note": f"fee only {M}0.016R (t {M}1.17); gross +0.023R (t +1.70)",
            },
            {
                "data": "Reference run, Jan 2020 to Sep 2024, 12 symbols",
                "n": "17,489",
                "net": f"{M}0.050R",
                "t": f"{M}5.51",
                "ci": f"[{M}0.074R, {M}0.027R]",
                "note": "gross +0.049R (t +5.55); no verdict, the rules were not selected for it",
            },
        ],
        "verdict": "No positive edge after costs.",
        "source": "Crossline backtest harness v1 (src/backtest), cost model cm1: research/trials.jsonl, "
        "docs/research/holdout-reference-macd_cross-2.2.0.json",
    },
    "macd_cross_ema50d_oppexit@1.0.0": {
        "title": "the shadow candidate",
        "rows": [
            {
                "data": "Selection data, Apr 2025 to Sep 2026 (already seen: the rules were picked on it)",
                "n": "2,642",
                "net": "+0.004R",
                "t": "+0.12",
                "ci": f"[{M}0.11R, +0.13R]",
                "note": "+0.042R under the review's cheaper costs; the review's +0.054R (t 2.11) included the "
                "EMA warm-up period, and +0.015R (t 0.61) with +10 bps impact",
            },
            {
                "data": "Pre-registered holdout, 2020 to Sep 2024, 12 symbols",
                "n": "7,354",
                "net": f"{M}0.024R",
                "t": f"{M}1.35",
                "ci": f"[{M}0.079R, +0.035R]",
                "note": "FAIL on all four pre-registered criteria; gross +0.081R (t 4.50)",
            },
        ],
        "verdict": "Failed its holdout: research only, no edge claimed.",
        "source": "docs/research/preregistration-macd_cross_ema50d_oppexit-1.0.0.md (§6), "
        "docs/research/holdout-result-macd_cross_ema50d_oppexit-1.0.0.md",
    },
}
# The live rules' two-year backtest, quoted on the overview and methodology pages
BASELINE_BACKTEST = {"period": "Nov 2024 to Sep 2026", **BASELINES["macd_cross@2.2.0"]["rows"][0]}


def base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def render(
    request: Request,
    template: str,
    page: str,
    title: str,
    description: str = SITE_DESCRIPTION,
    status_code: int = 200,
    noindex: bool = False,
    entry: str | None = None,
    **context,
) -> HTMLResponse:
    origin = base_url(request)
    response = templates.TemplateResponse(
        request,
        template,
        {
            "page": page,
            "title": title,
            "description": description,
            "canonical": origin + request.url.path,
            "origin": origin,
            "nav": NAV,
            "noindex": noindex,
            "entry": entry if entry is not None else page,
            "asset_version": ASSET_VERSION,
            "site_name": SITE_NAME,
            **context,
        },
        status_code=status_code,
    )
    if noindex:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def not_found(request: Request, message: str = "There is no signal with this id.") -> HTMLResponse:
    return render(
        request, "404.html", "not-found", f"Not found · {SITE_NAME}", status_code=404, noindex=True, entry="static",
        message=message,
    )


def cost_model_rows() -> list[dict]:
    rows = [{"symbol": sym, "bps": bps} for sym, bps in sorted(SLIPPAGE_BPS_BY_SYMBOL.items())]
    rows.append({"symbol": "Any other symbol", "bps": SLIPPAGE_BPS_DEFAULT})
    return rows


def build_router(is_authenticated: Callable[[Request], bool]) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    @router.api_route("/", methods=["GET", "HEAD"])
    async def overview(request: Request):
        return render(request, "overview.html", "overview", f"{SITE_NAME} · MACD crossover research signals",
                      baseline=BASELINE_BACKTEST)

    @router.get("/history")
    async def history_redirect():
        return RedirectResponse("/signals", status_code=301)

    @router.get("/signals")
    async def signals(request: Request):
        return render(request, "signals.html", "signals", f"Signals · {SITE_NAME}",
                      "Every recorded signal, open and closed, with filters by strategy, symbol, side, status and "
                      "source. Legacy, late and manual signals are listed and marked.")

    @router.get("/s/{signal_id}")
    async def signal_page(request: Request, signal_id: str):
        if not signal_id.isdigit() or len(signal_id) > 10:
            return not_found(request)
        sig = await get_signal_by_id(int(signal_id))
        if not sig:
            return not_found(request)
        sig.pop("journal", None)
        try:
            events = await get_signal_events(sig["id"]) or []
        except Exception:
            logger.exception("Could not read the events of signal %s", sig["id"])
            events = []
        window = None
        if not views.is_open(sig):
            persisted = await get_signal_candle_window(sig["id"])
            window = (persisted or {}).get("candles")
        title = views.page_title(sig)
        return render(
            request,
            "signal.html",
            "signal",
            title,
            views.og_description(sig),
            sig=sig,
            og_type="article",
            banner=views.banner_text(sig),
            outcome_class=views.outcome_class(sig),
            outcome_label=views.outcome_label(sig.get("win_status")),
            facts=views.signal_facts(sig, events, window),
            ref=views.strategy_ref(sig),
            strategy_status=views.strategy_status(sig),
            exclusion=views.exclusion_note(sig),
            is_open=views.is_open(sig),
        )

    @router.get("/track-record")
    async def track_record(request: Request):
        return render(
            request, "track_record.html", "track-record", f"Track record · {SITE_NAME}",
            "Track record per strategy version: n, mean net R with 95% interval, win rate against break-even, "
            "equity and drawdown in R, breakdowns and the full ledger with downloads.",
            baselines=BASELINES, holdouts=views.holdout_results(), min_breakdown=MIN_BREAKDOWN_SAMPLE,
            headline=f"{STRATEGY_KEY}@{STRATEGY_VERSION}",
        )

    @router.get("/status")
    async def status(request: Request):
        return render(request, "status.html", "status", f"Status · {SITE_NAME}",
                      "Scheduled scan health from the cron_runs table and the signal ledger's hash-chain head.")

    @router.get("/methodology")
    async def methodology(request: Request):
        strategies = views.strategy_versions(stored=await get_strategy_versions())
        return render(
            request, "methodology.html", "methodology", f"Methodology · {SITE_NAME}",
            "How Crossline measures results: R, gross, cost and net, the cost model, stop-first, expiry and time "
            "exits, win rate, confidence intervals, eligibility, pre-registration and holdout tests.",
            entry="static", strategies=[s for s in strategies if s.get("registered")],
            cost_rows=cost_model_rows(), fee_percent=ROUND_TRIP_FEE_PERCENT, cost_model=COST_MODEL_VERSION,
            stop_multiplier=STOP_SLIPPAGE_MULTIPLIER, min_sample=MIN_SAMPLE, min_totals=MIN_SAMPLE_FOR_TOTALS,
            min_breakdown=MIN_BREAKDOWN_SAMPLE, late_grace_minutes=LATE_SIGNAL_GRACE_SECONDS // 60,
            documents=views.research_documents(), baseline=BASELINE_BACKTEST,
        )

    @router.get("/changelog")
    async def changelog(request: Request):
        return render(request, "changelog.html", "changelog", f"Changelog · {SITE_NAME}",
                      "Every change to the strategy rules, cost model, ledger and public pages.",
                      entry="static", changelog=views.load_changelog())

    @router.get("/ops")
    async def ops(request: Request):
        return render(request, "ops.html", "ops", f"Ops · {SITE_NAME}", "Owner tools.", noindex=True,
                      signed_in=is_authenticated(request))

    @router.get("/research/{name}")
    async def research_document(name: str):
        path = views.research_file(name)
        if path is None:
            return Response("Not found", status_code=404, media_type="text/plain")
        media = "application/json" if path.suffix == ".json" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=300"})

    return router

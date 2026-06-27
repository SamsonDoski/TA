"""
Quant Desk REST API  —  thin HTTP layer over the existing Python engine.

This service does NOT reimplement any trading logic. It imports the real
strategy / backtest / portfolio / optimizer code and exposes the exact JSON
shapes that the Lovable front-end (src/lib/api.ts) expects:

    GET  /profiles            -> memory/stock_profile.json
    GET  /backtest            -> single-stock backtest (mirrors main.py)
    POST /optimize            -> grid search (mirrors engine/run_optimizer.py)
    POST /portfolio           -> multi-ticker sim (engine/portfolio.py)
    GET  /live/positions       -> read-only Alpaca positions + live signals

Run from the trading_assistant root:
    uvicorn api.server:app --reload --port 8000

Broker secrets stay server-side. /live/positions is strictly read-only and
never submits, modifies, or closes an order.
"""

import os
import sys
import math
from datetime import datetime, timedelta
from typing import List, Optional

# --- Make matplotlib headless BEFORE engine modules import it (portfolio.py
#     pulls in utils/visualize1.py which imports matplotlib.pyplot). ---
import matplotlib
matplotlib.use("Agg")

# --- Anchor to the project root so the engine's relative paths
#     ("data/", "memory/stock_profile.json") resolve no matter where uvicorn
#     was launched from. ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import itertools
import pandas as pd
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# --- The real engine (unchanged) ---
from utils.data_loader import fetch_data
from utils.profile_manager import load_profiles, is_stale
from strategies.moving_average import apply_moving_average_strategy
from strategies.rsi import apply_rsi_strategy
from strategies.ma_rsi_combo import apply_combo_strategy
from engine.backtest import BacktestEngine
from engine.portfolio import PortfolioSimulator
from strategy_config import get_profile

app = FastAPI(title="Quant Desk API", version="1.0.0")

# Allow the Lovable front-end (different origin) to call us. Tighten
# allow_origins to your deployed front-end URL in production.
ALLOWED = [o for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o]
# Lovable serves the published app on *.lovable.app and previews on
# *.lovableproject.com. Starlette's allow_origins does exact matches only, so
# wildcard subdomains must go through allow_origin_regex.
LOVABLE_ORIGIN_REGEX = r"https://([a-z0-9-]+\.)*(lovable\.app|lovableproject\.com)"
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED,
    allow_origin_regex=LOVABLE_ORIGIN_REGEX,
    allow_methods=["*"],
    allow_headers=["*"],
)

BUFFER_DAYS = 730  # indicator warm-up runway, matching main.py / run_optimizer.py


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(v):
    """Make a single value JSON-safe (NaN/inf -> None, numpy -> python)."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    try:
        import numpy as np
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        if isinstance(v, (np.bool_,)):
            return bool(v)
    except Exception:
        pass
    return v


def _apply_strategy(df, strategy, p):
    """Route to the real strategy fn using a profile-settings dict `p`."""
    if strategy == "MA":
        return apply_moving_average_strategy(
            df, short_window=p["short_window"], long_window=p["long_window"],
            stop_loss_pct=p["stop_loss_pct"])
    if strategy == "RSI":
        return apply_rsi_strategy(
            df, rsi_window=p["rsi_window"], overbought=p["overbought"],
            oversold=p["oversold"], stop_loss_pct=p["stop_loss_pct"])
    # default Combo
    return apply_combo_strategy(
        df, short_window=p["short_window"], long_window=p["long_window"],
        rsi_window=p["rsi_window"], overbought=p["overbought"],
        oversold=p["oversold"], stop_loss_pct=p["stop_loss_pct"])


def _metrics(engine, df_bt):
    """Numeric summary reusing the engine's own formulas (no string parsing)."""
    final_equity = float(df_bt["Equity"].iloc[-1])
    net_profit = final_equity - engine.initial_equity
    total_return = final_equity / engine.initial_equity - 1
    max_dd = float(engine._max_drawdown(df_bt["Equity"]))
    win_rate = float(engine._win_rate(df_bt))
    num_trades = int(((df_bt["Signal"] == 1) & (df_bt["Signal"].shift(1) != 1)).sum())
    return {
        "total_return_pct": round(total_return * 100, 2),
        "net_profit": round(net_profit, 2),
        "final_balance": round(final_equity, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "win_rate_pct": round(win_rate * 100, 2),
        "num_trades": num_trades,
    }


def _trade_log(df_bt):
    """Derive entry/exit pairs from signal transitions for the UI trade table."""
    sig = df_bt["Signal"].values
    closes = df_bt["Close"].values
    idx = df_bt.index
    trades, entry_i = [], None
    for i in range(len(sig)):
        prev = sig[i - 1] if i > 0 else 0
        if sig[i] == 1 and prev != 1:
            entry_i = i
        elif sig[i] == 0 and prev == 1 and entry_i is not None:
            ep, xp = float(closes[entry_i]), float(closes[i])
            trades.append({
                "entry_date": str(pd.Timestamp(idx[entry_i]).date()),
                "exit_date": str(pd.Timestamp(idx[i]).date()),
                "entry_price": round(ep, 2),
                "exit_price": round(xp, 2),
                "pl_pct": round((xp - ep) / ep * 100, 2) if ep else None,
                "pl": round(xp - ep, 2),
                "holding_days": int((pd.Timestamp(idx[i]) - pd.Timestamp(idx[entry_i])).days),
            })
            entry_i = None
    return trades


def _run_single_backtest(ticker, start, end, strategy, profile, equity):
    """Mirrors main.py: buffered fetch -> strategy -> ghost-trade slice -> backtest."""
    ticker = ticker.upper()
    requested_start = datetime.strptime(start, "%Y-%m-%d")
    fetch_start = (requested_start - timedelta(days=BUFFER_DAYS)).strftime("%Y-%m-%d")

    df = fetch_data(ticker, start=fetch_start, end=end)
    if df.empty:
        raise HTTPException(404, f"No data found for {ticker} in range.")

    p = get_profile(profile)
    df = _apply_strategy(df, strategy, p)

    # Ghost-trade fix (identical to main.py)
    df["Buy_Trigger"] = (df["Signal"] == 1) & (df["Signal"].shift(1) == 0)
    df_win = df.loc[start:end].copy()
    df_win.loc[df_win["Buy_Trigger"].cumsum() == 0, "Signal"] = 0
    if df_win.empty:
        raise HTTPException(404, f"No rows for {ticker} in {start}..{end}.")

    engine = BacktestEngine(initial_equity=equity)
    df_bt = engine.run(df_win)
    return ticker, engine, df_bt, df_win


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class OptimizeBody(BaseModel):
    ticker: str
    start: str = "2020-01-01"
    end: str = "2024-01-01"
    strategy: str = "Combo"
    equity: float = 10000.0
    short_windows: Optional[List[int]] = None
    long_windows: Optional[List[int]] = None


class PortfolioBody(BaseModel):
    tickers: List[str] = Field(..., min_items=1)
    start: str = "2020-01-01"
    end: str = "2024-01-01"
    equity: float = 100000.0
    strategy: str = "Combo"
    profile: str = "Swing"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "service": "quant-desk-api"}


@app.get("/profiles")
def get_profiles():
    """The optimized per-stock memory (memory/stock_profile.json)."""
    profiles = load_profiles()
    out = []
    for ticker, r in profiles.items():
        out.append({
            "ticker": ticker,
            "strategy": r.get("strategy", "Combo"),
            "best_short_window": r.get("best_short_window"),
            "best_long_window": r.get("best_long_window"),
            "rsi_period": r.get("rsi_period", 14),
            "last_optimized": r.get("last_optimized"),
            "stale": is_stale(ticker),
        })
    return out


@app.get("/backtest")
def get_backtest(
    ticker: str,
    start: str = Query("2020-01-01"),
    end: str = Query("2024-01-01"),
    strategy: str = Query("Combo"),
    profile: str = Query("Swing"),
    equity: float = Query(10000.0),
):
    ticker, engine, df_bt, _ = _run_single_backtest(
        ticker, start, end, strategy, profile, equity)

    cols = ["Close", "MA_short", "MA_long", "RSI", "Signal", "Buy_Trigger", "Equity"]
    rows = []
    for ts, row in df_bt.iterrows():
        r = {"date": str(pd.Timestamp(ts).date())}
        for c in cols:
            r[c] = _clean(row[c]) if c in df_bt.columns else None
        rows.append(r)

    return {
        "ticker": ticker,
        "strategy": strategy,
        "profile": profile,
        "rows": rows,
        "trades": _trade_log(df_bt),
        "summary": _metrics(engine, df_bt),
    }


@app.get("/stock/{ticker}")
def stock_detail(
    ticker: str,
    start: str = Query(None),
    end: str = Query(None),
    equity: float = Query(10000.0),
):
    """
    Per-ticker "bot thought process" view. Plots price + the stock's OWN optimized
    moving averages (from memory/stock_profile.json — the exact windows the live bot
    trades on) + RSI, with the buy/sell signals the bot acts on. Read-only, cached data.
    """
    ticker = ticker.upper()
    profiles = load_profiles()
    rules = profiles.get(ticker, {})
    short_ma = rules.get("best_short_window") or 50
    long_ma = rules.get("best_long_window") or 100
    rsi_period = rules.get("rsi_period", 14)

    end = end or datetime.now().strftime("%Y-%m-%d")
    start = start or (datetime.now() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    requested_start = datetime.strptime(start, "%Y-%m-%d")
    fetch_start = (requested_start - timedelta(days=BUFFER_DAYS)).strftime("%Y-%m-%d")

    df = fetch_data(ticker, start=fetch_start, end=end, allow_download=False)
    if df.empty:
        raise HTTPException(404, f"No cached data for {ticker}.")

    # Same call shape the live bot uses (combo defaults for overbought/oversold/stop).
    df = apply_combo_strategy(df, short_window=short_ma, long_window=long_ma, rsi_window=rsi_period)
    df["Buy_Trigger"] = (df["Signal"] == 1) & (df["Signal"].shift(1) == 0)
    df_win = df.loc[start:end].copy()
    if df_win.empty:
        raise HTTPException(404, f"No rows for {ticker} in {start}..{end}.")
    df_win.loc[df_win["Buy_Trigger"].cumsum() == 0, "Signal"] = 0

    engine = BacktestEngine(initial_equity=equity)
    df_bt = engine.run(df_win)

    cols = ["Close", "MA_short", "MA_long", "RSI", "Signal", "Buy_Trigger", "Equity"]
    rows = []
    for ts, row in df_bt.iterrows():
        r = {"date": str(pd.Timestamp(ts).date())}
        for c in cols:
            r[c] = _clean(row[c]) if c in df_bt.columns else None
        rows.append(r)

    return {
        "ticker": ticker,
        "strategy": "Combo",
        "optimized": ticker in profiles,
        "short_window": short_ma,
        "long_window": long_ma,
        "rsi_period": rsi_period,
        "last_optimized": rules.get("last_optimized"),
        "current_signal": rows[-1]["Signal"] if rows else None,
        "rows": rows,
        "trades": _trade_log(df_bt),
        "summary": _metrics(engine, df_bt),
    }


@app.get("/optimize")
def optimize_get(
    ticker: str,
    start: str = Query("2020-01-01"),
    end: str = Query("2024-01-01"),
    strategy: str = Query("Combo"),
    equity: float = Query(10000.0),
):
    """GET variant of the grid search (uses the default sweep ranges)."""
    return optimize(OptimizeBody(
        ticker=ticker, start=start, end=end, strategy=strategy, equity=equity))


@app.post("/optimize")
def optimize(body: OptimizeBody):
    """
    Grid search returning the full heatmap grid + best pair.

    Reuses the real primitives (fetch_data + apply_*_strategy + BacktestEngine)
    in the same loop shape as engine/run_optimizer.py, but returns every cell
    so the UI can draw the heatmap and leaderboard.
    """
    ticker = body.ticker.upper()
    short_mas = body.short_windows or [5, 10, 20, 30, 40, 50]
    long_mas = body.long_windows or [10, 30, 50, 100, 150, 200, 250, 300, 350, 400]

    requested_start = datetime.strptime(body.start, "%Y-%m-%d")
    fetch_start = (requested_start - timedelta(days=BUFFER_DAYS)).strftime("%Y-%m-%d")
    df_raw = fetch_data(ticker, start=fetch_start, end=body.end)
    if df_raw.empty:
        raise HTTPException(404, f"No data found for {ticker}.")

    grid = []
    for short_ma, long_ma in itertools.product(short_mas, long_mas):
        if short_ma >= long_ma:
            continue
        df_test = df_raw.copy()
        if body.strategy == "MA":
            df_test = apply_moving_average_strategy(
                df_test, short_window=short_ma, long_window=long_ma, stop_loss_pct=-0.15)
        else:  # Combo
            df_test = apply_combo_strategy(
                df_test, short_window=short_ma, long_window=long_ma, rsi_window=14,
                overbought=70, oversold=30, stop_loss_pct=-0.15)

        df_test["Buy_Trigger"] = (df_test["Signal"] == 1) & (df_test["Signal"].shift(1) == 0)
        df_win = df_test.loc[body.start:body.end].copy()
        if df_win.empty:
            continue
        df_win.loc[df_win["Buy_Trigger"].cumsum() == 0, "Signal"] = 0

        engine = BacktestEngine(initial_equity=body.equity)
        df_bt = engine.run(df_win)
        final_equity = float(df_bt["Equity"].iloc[-1])
        total_return = (final_equity - body.equity) / body.equity
        max_dd = float(engine._max_drawdown(df_bt["Equity"]))
        score = 0.0 if max_dd == 0 else total_return / abs(max_dd)
        grid.append({
            "short": short_ma,
            "long": long_ma,
            "return_pct": round(total_return * 100, 2),
            "drawdown_pct": round(max_dd * 100, 2),
            "win_rate_pct": round(float(engine._win_rate(df_bt)) * 100, 2),
            "score": round(score, 3),
        })

    if not grid:
        raise HTTPException(404, "No valid parameter combinations produced results.")

    grid.sort(key=lambda x: x["return_pct"], reverse=True)
    best = {"short": grid[0]["short"], "long": grid[0]["long"]}
    return {"ticker": ticker, "strategy": body.strategy, "grid": grid, "best": best}


DEFAULT_PORTFOLIO_TICKERS = "NVDA,AAPL,MSFT,AMZN,GOOGL,META,TSLA,NFLX"


@app.get("/portfolio")
def portfolio_get(
    tickers: str = Query(DEFAULT_PORTFOLIO_TICKERS),
    start: str = Query("2020-01-01"),
    end: str = Query("2024-01-01"),
    equity: float = Query(100000.0),
    strategy: str = Query("Combo"),
    profile: str = Query("Swing"),
):
    """GET variant so the dashboard can load the portfolio without a POST body."""
    tlist = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    return portfolio(PortfolioBody(
        tickers=tlist, start=start, end=end,
        equity=equity, strategy=strategy, profile=profile))


@app.post("/portfolio")
def portfolio(body: PortfolioBody):
    """Multi-ticker simulation via the real PortfolioSimulator (no plotting)."""
    tickers = [t.upper() for t in body.tickers]
    sim = PortfolioSimulator(
        tickers=tickers, start_date=body.start, end_date=body.end,
        initial_equity=body.equity, profile=body.profile, strategy=body.strategy)
    try:
        df_port = sim.run_simulation()
    except Exception as e:
        # e.g. pd.concat on empty list when every ticker's data fetch failed.
        raise HTTPException(502, f"No price data available for {tickers}: {e}")
    if df_port.empty or "Portfolio_Equity" not in df_port.columns:
        raise HTTPException(404, "Simulation produced no data for the given tickers/range.")

    # Global equity curve + drawdown
    eq = df_port["Portfolio_Equity"]
    roll_max = eq.cummax()
    dd = (eq / roll_max - 1.0) * 100
    equity_curve = [
        {"date": str(pd.Timestamp(ts).date()),
         "equity": _clean(round(float(eq.loc[ts]), 2)),
         "drawdown_pct": _clean(round(float(dd.loc[ts]), 2))}
        for ts in df_port.index
    ]

    # Per-asset breakdown from the simulator's stored frames
    allocated = body.equity / len(sim.portfolio_data) if sim.portfolio_data else body.equity
    final_total = float(eq.iloc[-1])
    assets = []
    for ticker, df in sim.portfolio_data.items():
        stock_final = float(df["Equity"].iloc[-1])
        profit = stock_final - allocated
        a_roll = df["Equity"].cummax()
        a_dd = float((df["Equity"] / a_roll - 1.0).min()) * 100
        buys = int(((df["Signal"] == 1) & (df["Signal"].shift(1) != 1)).sum())
        assets.append({
            "ticker": ticker.upper(),
            "allocation_pct": round(allocated / body.equity * 100, 2),
            "final_value": round(stock_final, 2),
            "pl": round(profit, 2),
            "pl_pct": round(profit / allocated * 100, 2) if allocated else None,
            "contribution_pct": round(profit / body.equity * 100, 2),
            "max_drawdown_pct": round(a_dd, 2),
            "num_trades": buys,
        })
    assets.sort(key=lambda x: x["pl"], reverse=True)

    total_return = (final_total - body.equity) / body.equity * 100
    return {
        "tickers": tickers,
        "strategy": body.strategy,
        "profile": body.profile,
        "equity_curve": equity_curve,
        "assets": assets,
        "summary": {
            "total_return_pct": round(total_return, 2),
            "net_profit": round(final_total - body.equity, 2),
            "final_balance": round(final_total, 2),
            "peak_balance": round(float(eq.max()), 2),
            "max_drawdown_pct": round(float(dd.min()), 2),
            "top_performer": assets[0]["ticker"] if assets else None,
        },
    }


NOTIONAL_ALLOCATION = 5000.0  # matches live_controller.py


def _alpaca_client():
    """Build a paper TradingClient, or None if keys are missing/unavailable."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        from alpaca.trading.client import TradingClient
        api_key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        if api_key and secret:
            return TradingClient(api_key, secret, paper=True)
    except Exception as e:
        print(f"[live] Alpaca client init failed: {e}")
    return None


def _alpaca_positions(client):
    """(positions list, owned dict). Read-only."""
    positions, owned = [], {}
    if client is None:
        return positions, owned
    try:
        for pos in client.get_all_positions():
            owned[pos.symbol] = pos
            positions.append({
                "symbol": pos.symbol,
                "qty": _clean(float(pos.qty)),
                "avg_entry": _clean(round(float(pos.avg_entry_price), 2)),
                "current_price": _clean(round(float(pos.current_price), 2)),
                "market_value": _clean(round(float(pos.market_value), 2)),
                "unrealized_pl": _clean(round(float(pos.unrealized_pl), 2)),
                "unrealized_pl_pct": _clean(round(float(pos.unrealized_plpc) * 100, 2)),
            })
    except Exception as e:
        print(f"[live] positions fetch failed: {e}")
    return positions, owned


def _alpaca_activity(client, limit=25):
    """Recent REAL order history for the activity feed. Read-only — never trades."""
    activity = []
    if client is None:
        return activity
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit))
        for o in orders:
            side = getattr(o.side, "value", str(o.side)).upper()
            status = getattr(o.status, "value", str(o.status))
            ts = o.filled_at or o.submitted_at
            qty = o.filled_qty or o.qty
            price = o.filled_avg_price
            emoji = "🚀" if side == "BUY" else "🛑"
            if price:
                msg = f"{emoji} {side} {qty} {o.symbol} @ ${float(price):,.2f} · {status}"
            else:
                msg = f"{emoji} {side} {o.symbol} · {status}"
            activity.append({
                "ts": ts.isoformat() if ts else None,
                "symbol": o.symbol,
                "side": side,
                "qty": _clean(float(qty)) if qty else None,
                "price": _clean(round(float(price), 2)) if price else None,
                "status": status,
                "message": msg,
            })
    except Exception as e:
        print(f"[live] activity fetch failed: {e}")
    return activity


def _compute_signals(owned):
    """Today's signal per tracked stock (read-only, mirrors live_controller logic)."""
    profiles = load_profiles()
    signals = []
    for ticker, rules in profiles.items():
        if is_stale(ticker):
            signals.append({"ticker": ticker, "signal": "STALE",
                            "short_window": rules.get("best_short_window"),
                            "long_window": rules.get("best_long_window")})
            continue
        short_ma = rules["best_short_window"]
        long_ma = rules["best_long_window"]
        rsi_period = rules.get("rsi_period", 14)
        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=(long_ma * 2) + 365)).strftime("%Y-%m-%d")
            # Cached-only: the live view must be fast; never trigger 24 downloads.
            df = fetch_data(ticker, start, end, allow_download=False)
            if df.empty or len(df) < 3:
                continue
            df = apply_combo_strategy(df, short_window=short_ma,
                                      long_window=long_ma, rsi_window=rsi_period)
            latest = df.iloc[-2]["Signal"]
            prev = df.iloc[-3]["Signal"]
            price = float(df.iloc[-2]["Close"])
            rsi = float(df.iloc[-2]["RSI"])
            already = ticker in owned

            if latest == 1 and prev == 0 and not already:
                status = "BUY"
            elif latest == 0 and already:
                status = "SELL"
            elif latest == 1 and already:
                status = "HOLDING"
            elif latest == 1 and prev == 1 and not already:
                status = "MISSED_DIP"
            else:
                status = "WAITING"

            entry = {
                "ticker": ticker, "signal": status,
                "price": _clean(round(price, 2)), "rsi": _clean(round(rsi, 1)),
                "short_window": short_ma, "long_window": long_ma,
            }
            if already:
                entry["pl_pct"] = _clean(round(float(owned[ticker].unrealized_plpc) * 100, 2))
            signals.append(entry)
        except Exception as e:
            print(f"[live] signal calc failed for {ticker}: {e}")
            continue
    return signals


@app.get("/live")
@app.get("/live/positions")
def live_positions():
    """
    READ-ONLY live view: current Alpaca paper positions, today's signals, and
    the real recent-order activity feed. NEVER submits or closes an order.
    """
    client = _alpaca_client()
    positions, owned = _alpaca_positions(client)
    signals = _compute_signals(owned)
    activity = _alpaca_activity(client)
    return {
        "mode": "paper",
        "notional_allocation": NOTIONAL_ALLOCATION,
        "positions": positions,
        "signals": signals,
        "activity": activity,
    }


@app.get("/live/dry-run")
@app.post("/live/run")
def live_dry_run():
    """
    PREVIEW what the live pipeline WOULD do right now, given current positions
    and signals. Submits NOTHING, posts to NO webhook. 100% read-only — safe to
    expose publicly. Real execution stays in live_controller.py (run manually).
    """
    client = _alpaca_client()
    _, owned = _alpaca_positions(client)
    signals = _compute_signals(owned)

    actions = []
    for s in signals:
        st, t, pl = s["signal"], s["ticker"], s.get("pl_pct")
        pl_str = f" (P/L {pl:+.2f}%)" if pl is not None else ""
        if st == "BUY":
            would, msg = True, f"🚀 Would BUY ${NOTIONAL_ALLOCATION:,.0f} of {t} — fresh entry signal"
        elif st == "SELL":
            would, msg = True, f"🛑 Would SELL / liquidate {t}{pl_str} — trend broke"
        elif st == "HOLDING":
            would, msg = False, f"⏳ Would hold {t}{pl_str}"
        elif st == "MISSED_DIP":
            would, msg = False, f"⏳ {t}: trend up but missed the RSI dip — waiting for reset"
        elif st == "STALE":
            would, msg = False, f"⚠️ {t}: profile stale — would skip"
        else:  # WAITING
            would, msg = False, f"⏳ {t}: waiting — trend negative or RSI too high"
        actions.append({
            "ticker": t, "action": st, "would_execute": would, "message": msg,
            "price": s.get("price"), "rsi": s.get("rsi"), "pl_pct": pl,
        })

    return {
        "dry_run": True,
        "generated_at": datetime.now().isoformat(),
        "notional_allocation": NOTIONAL_ALLOCATION,
        "summary": {
            "would_buy": sum(1 for a in actions if a["action"] == "BUY"),
            "would_sell": sum(1 for a in actions if a["action"] == "SELL"),
            "holding": sum(1 for a in actions if a["action"] == "HOLDING"),
            "waiting": sum(1 for a in actions if a["action"] in ("WAITING", "MISSED_DIP")),
            "total": len(actions),
        },
        "actions": actions,
    }

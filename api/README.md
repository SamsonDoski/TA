# Quant Desk API

A thin FastAPI layer over the existing trading engine. It imports and calls the
real strategy / backtest / portfolio / optimizer code — **no trading logic is
duplicated or rewritten** — and exposes the JSON shapes the Lovable front-end
(`src/lib/api.ts`) expects.

## Endpoints

| Method | Path | Backed by |
|--------|------|-----------|
| `GET`  | `/health` | — |
| `GET`  | `/profiles` | `memory/stock_profile.json` |
| `GET`  | `/backtest?ticker=NVDA&start=&end=&strategy=&profile=&equity=` | `main.py` flow |
| `POST` | `/optimize` | `engine/run_optimizer.py` grid |
| `POST` | `/portfolio` | `engine/portfolio.py` |
| `GET`  | `/live/positions` | Alpaca (read-only) + live signals |

`/live/positions` is **strictly read-only** — it reads positions and computes
today's signal per stock, but never submits, modifies, or closes an order.
Order execution stays in `live_controller.py`. Broker keys never leave the
server (loaded from `.env`, never returned in any response).

## Run it

From the `trading_assistant` root:

```bash
# install the API deps on top of the existing requirements
pip install -r api/requirements-api.txt

# start the server (interactive docs at http://localhost:8000/docs)
uvicorn api.server:app --reload --port 8000
```

> This repo's `venv` was created under WSL. Launch the server from a WSL shell
> (`./venv/bin/python -m uvicorn api.server:app --reload --port 8000`) or use any
> Python 3.12 environment that has `requirements.txt` + `api/requirements-api.txt`
> installed.

## Connect the front-end

In the Lovable project's `.env`:

```
VITE_API_BASE_URL=http://localhost:8000
```

`src/lib/api.ts` will then hit these endpoints and fall back to mock data only on
error or when the var is unset.

## Notes

- CORS defaults to `*`. In production set `FRONTEND_ORIGINS=https://your-app.lovable.app`
  (comma-separated) before launching.
- Backtest/optimize use a 730-day fetch buffer for indicator warm-up, identical
  to `main.py` / `engine/run_optimizer.py`.
- Heavy calls (optimize grid, multi-ticker portfolio) run synchronously; FastAPI
  executes them in a threadpool so the event loop stays responsive.

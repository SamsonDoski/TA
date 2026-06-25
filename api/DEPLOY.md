# Deploying the Quant Desk API

Goal: get `https://strategy-suite-dash.lovable.app` pulling **live** data by
hosting this FastAPI service at a public URL.

Files used: [`Dockerfile`](../Dockerfile), [`.dockerignore`](../.dockerignore),
[`render.yaml`](../render.yaml).

---

## 0. Push to GitHub (one time)

Render/Railway deploy from a repo. From the `trading_assistant` folder:

```bash
git add Dockerfile .dockerignore render.yaml api/ start_api.sh start_api.ps1
git commit -m "Add deployable FastAPI layer + Docker"
git push        # to your GitHub remote
```

`.env` stays out of git (it's in `.gitignore`) — secrets go in the host's
dashboard instead.

---

## Option A — Render (genuinely free web tier) ✅ recommended

1. Go to **dashboard.render.com** → **New +** → **Blueprint**.
2. Connect the GitHub repo. Render reads `render.yaml` and proposes the
   `quant-desk-api` web service (Docker, free plan, health check `/health`).
3. Click **Apply**. When prompted, paste the secret env vars:
   - `ALPACA_API_KEY`
   - `ALPACA_SECRET_KEY`
   - `ALPACA_BASE_URL`  (e.g. `https://paper-api.alpaca.markets`)
   - `FRONTEND_ORIGINS` is pre-filled to your Lovable URL.
4. First build takes a few minutes. You'll get a URL like
   `https://quant-desk-api.onrender.com`.
5. Verify: open `https://quant-desk-api.onrender.com/health` → `{"status":"ok"}`
   and `/docs` for the interactive API.

**Free-tier caveat:** the instance sleeps after ~15 min idle. The first request
after sleeping cold-starts the container (pandas/matplotlib import ≈ 15–30 s), so
the UI may briefly show mock data (it falls back on timeout), then real data once
warm. Fine for a demo; upgrade to a paid instance to keep it always-on.

### Without the blueprint (manual)
New + → **Web Service** → connect repo → Runtime **Docker** → Plan **Free** →
add the 4 env vars above → Create.

---

## Option B — Railway (fast, uses trial credit)

Railway no longer has a perpetual free tier — it gives one-time trial credit, then
~$5/mo. If that's acceptable:

1. **railway.app** → **New Project** → **Deploy from GitHub repo**.
2. Railway auto-detects the `Dockerfile`.
3. **Variables** tab → add `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
   `ALPACA_BASE_URL`, `FRONTEND_ORIGINS=https://strategy-suite-dash.lovable.app`.
4. **Settings → Networking → Generate Domain** → you get a public URL.
5. Verify `/<domain>/health`.

(Fly.io is another free-allowance option: `fly launch` from this folder reuses the
same Dockerfile.)

---

## Final step — point the front-end at the API

In the **Lovable** project:

1. Project **Settings → Environment variables**.
2. Set `VITE_API_BASE_URL = https://quant-desk-api.onrender.com`
   (your deployed URL, **no trailing slash**).
3. **Republish** the front-end.

`src/lib/api.ts` will now hit the live API and only fall back to mock data on
error/timeout. Confirm by loading the dashboard — **Open Positions** should show
your real Alpaca paper count (e.g. 13) instead of 0.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| UI still shows mock data | `VITE_API_BASE_URL` unset/typo, or front-end not republished. |
| CORS error in browser console | `FRONTEND_ORIGINS` must exactly match the Lovable origin (scheme + host, no path). |
| `/live/positions` returns empty positions | Alpaca env vars missing/wrong on the host. Signals still work (they only need price data). |
| First load very slow then works | Free-tier cold start — expected. Keep-warm with a paid plan or an uptime pinger. |

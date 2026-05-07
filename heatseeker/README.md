# Heatseeker™ — Deploy Guide (Browser Only)

No downloads. No installs. Everything done in a browser tab.

---

## Step 1 — GitHub (2 min)

1. Go to **github.com** → Sign in or create a free account
2. Click **"New repository"** → name it `heatseeker` → **Create repository**
3. On the repo page, click **"uploading an existing file"**
4. **Drag the entire unzipped `heatseeker` folder** into the upload area
5. Click **"Commit changes"** → your code is now on GitHub

---

## Step 2 — Railway (3 min)

1. Go to **railway.app** → Sign in with GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your `heatseeker` repository
4. Railway auto-detects Node.js and runs:
   - `npm install`
   - `npm run build` (builds the React frontend)
   - `npm start` (starts the Express server)
5. Click **"Generate Domain"** under Settings → Networking
6. Your site is live at `https://heatseeker-xyz.up.railway.app` 🎉

Railway's free tier gives you **500 hours/month** — enough for all-day trading hours.

---

## How It Works

```
Your Browser  ←─ HTTPS ─→  Railway Server  ←─ DXLink WS ─→  Tastytrade API
```

- Your Tastytrade credentials are sent over HTTPS to the Railway server, which
  forwards them directly to Tastytrade. They are never logged or stored to disk.
- The server holds the DXLink WebSocket connection and pushes computed
  GEX/Vanna data to your browser every 2 seconds via Server-Sent Events (SSE).
- Closing the browser tab automatically disconnects the stream.

---

## Security Note

Since this is a personal tool, consider adding HTTP Basic Auth to the Railway
deployment (Settings → Variables → add `BASIC_AUTH_USER` and `BASIC_AUTH_PASS`)
so only you can access the URL. Ask Claude to add that if you want it.

---

## Formula Reference

| Metric | Formula |
|--------|---------|
| **GEX** | `(Call_OI × Call_γ − Put_OI × Put_γ) × Spot × 100` |
| **Vanna** | `Vega × \|Delta\| / (Spot × ImpliedVol)` (per-strike aggregate) |
| **King Node** | Strike with highest absolute GEX value |
| **Gatekeeper** | Significant node between current price and King Node |

---

⚠️ Not financial advice. For educational purposes only.

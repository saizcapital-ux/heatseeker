# Gamma Flow Terminal

Browser-based dealer gamma exposure (GEX) dashboard for SPX/VIX/SPY options.  
Data: CBOE 15-min delayed quotes, no API key required.  
Deploy: **zero local tools needed** — drag-and-drop or GitHub web editor.

## Deploy (Method 1 — Drag & Drop)

1. Download/zip this `gamma-terminal/` folder
2. Go to [app.netlify.com](https://app.netlify.com)
3. Drag the folder onto the "Deploy manually" drop zone
4. Done — your terminal is live at a `.netlify.app` URL

## Deploy (Method 2 — GitHub + Netlify)

1. Go to [github.com/new](https://github.com/new) and create a new repo
2. Click **"uploading an existing file"** and upload all files in this folder (preserving the `netlify/functions/` path)
3. Go to [app.netlify.com](https://app.netlify.com) → "Add new site" → "Import an existing project"
4. Connect your GitHub repo → Deploy
5. Netlify auto-detects `netlify.toml` — no build settings needed

## File structure

```
gamma-terminal/
├── index.html              # Full UI — loads engine.js + Plotly from CDN
├── engine.js               # Pure analytics: GEX, gamma flip, max pain, BS greeks
├── netlify.toml            # Functions directory + /api/chain redirect
├── netlify/
│   └── functions/
│       └── chain.js        # CORS proxy for CBOE feed (Node 18+, no npm deps)
└── README.md
```

## What it shows

- **Regime banner**: POSITIVE GAMMA (pinning) vs NEGATIVE GAMMA (trending)
- **Gamma flip**: strike where cumulative dealer GEX crosses zero — the regime boundary
- **Call wall / Put wall**: heaviest OI strikes above/below spot
- **Max pain**: expiry price that maximizes losses to option holders
- **GEX by strike**: hero chart with spot and flip horizon lines
- **GEX surface**: 3D strike × expiry × GEX visualization
- **Dealer toggle**: flip the long-calls assumption and see GEX invert live

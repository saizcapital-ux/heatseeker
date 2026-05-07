import { useState, useEffect, useRef, useCallback } from 'react';
import {
  BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer,
  ReferenceLine, ComposedChart, Area, CartesianGrid,
} from 'recharts';
import { loadChain, openStream, logout } from '../api.js';

const SYMBOLS   = ['SPX','NDX','SPY','QQQ','IWM'];
const DTE_OPTS  = [{label:'0DTE',value:0},{label:'1DTE',value:1},{label:'7DTE',value:7}];
const gexColor  = v => v >= 0 ? '#0aff9d' : '#ff3a5c';

function Badge({ label, dir }) {
  const s = {
    BULL:{ color:'#0aff9d', bg:'rgba(10,255,157,0.08)', border:'rgba(10,255,157,0.22)', icon:'▲' },
    BEAR:{ color:'#ff3a5c', bg:'rgba(255,58,92,0.08)',  border:'rgba(255,58,92,0.22)',  icon:'▼' },
    NEUT:{ color:'#888',    bg:'rgba(120,120,120,0.08)',border:'rgba(120,120,120,0.2)', icon:'◆' },
  }[dir] || {};
  return (
    <div style={{ display:'flex', alignItems:'center', gap:5, padding:'3px 10px', background:s.bg, border:`1px solid ${s.border}`, borderRadius:3, fontSize:11 }}>
      <span style={{ color:'#557', fontSize:10 }}>{label}</span>
      <span style={{ color:s.color, fontWeight:700 }}>{s.icon} {dir}</span>
    </div>
  );
}

const PHead = ({ title, right }) => (
  <div style={{ padding:'9px 14px', borderBottom:'1px solid rgba(255,255,255,0.05)', display:'flex', justifyContent:'space-between', alignItems:'center', flexShrink:0 }}>
    <span style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:11, fontWeight:600, letterSpacing:'0.1em', color:'#7a9bc0' }}>{title}</span>
    {right && <span style={{ fontSize:10, color:'#334' }}>{right}</span>}
  </div>
);

export default function Dashboard({ onLogout }) {
  const [symbol, setSymbol]       = useState('SPX');
  const [dte, setDte]             = useState(0);
  const [status, setStatus]       = useState('Choose a symbol and press START.');
  const [loading, setLoading]     = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);

  const [spot, setSpot]             = useState(null);
  const [nodes, setNodes]           = useState([]);
  const [kingNode, setKingNode]     = useState(null);
  const [gatekeepers, setGatekeepers] = useState([]);
  const [signals, setSignals]       = useState({ SPX:'NEUT', SPY:'NEUT', QQQ:'NEUT' });
  const [priceHistory, setPriceHist]= useState([]);
  const [reshuffling, setReshuffle] = useState(false);

  const closeRef    = useRef(null);
  const prevKingRef = useRef(null);

  const startStream = useCallback(async () => {
    closeRef.current?.();
    setLoading(true); setStreaming(false);
    setStatus('Loading options chain...');

    try {
      const chain = await loadChain(symbol, dte);
      setStatus(`✓ Chain: ${chain.strikeCount} strikes · ${chain.dte ?? 0}DTE · exp ${chain.expirationDate}`);

      const close = openStream(symbol, {
        onUpdate(p) {
          setSpot(p.spot);

          // Detect king node reshuffle
          if (p.kingNode && prevKingRef.current && prevKingRef.current !== p.kingNode.strike) {
            setReshuffle(true);
            setTimeout(() => setReshuffle(false), 900);
          }
          prevKingRef.current = p.kingNode?.strike ?? null;

          setNodes(p.nodes || []);
          setKingNode(p.kingNode);
          setGatekeepers(p.gatekeepers || []);
          setSignals(p.signals || { SPX:'NEUT', SPY:'NEUT', QQQ:'NEUT' });
          setLastUpdate(new Date(p.timestamp));

          setPriceHist(prev => {
            const t = new Date(p.timestamp);
            const lbl = `${t.getHours()}:${String(t.getMinutes()).padStart(2,'0')}`;
            return [...prev.slice(-119), { time:lbl, price:p.spot }];
          });

          setStreaming(true);
          setLoading(false);
        },
        onStatus: setStatus,
        onError(msg) { setStatus(`⚠ ${msg}`); setLoading(false); },
      });

      closeRef.current = close;
    } catch (err) {
      setStatus(`Error: ${err.message}`); setLoading(false);
    }
  }, [symbol, dte]);

  useEffect(() => () => closeRef.current?.(), []);

  const handleLogout = async () => {
    closeRef.current?.();
    await logout();
    onLogout();
  };

  // Derived values
  const firstPrice   = priceHistory[0]?.price ?? spot;
  const spotChange   = spot && firstPrice ? spot - firstPrice : 0;
  const isUp         = spotChange >= 0;
  const maxGex       = nodes.length ? Math.max(...nodes.map(n => Math.abs(n.gex)))   : 1;
  const aligned      = Object.values(signals).every(s => s === signals.SPX);
  const prices       = priceHistory.map(p => p.price);
  const chartMin     = prices.length ? Math.min(...prices) - 8 : 0;
  const chartMax     = prices.length ? Math.max(...prices) + 8 : 1;

  const nodeType = s => {
    if (kingNode?.strike === s) return 'king';
    if (gatekeepers.find(g => g.strike === s)) return 'gate';
    return null;
  };

  const stepSize = nodes.length > 1 ? (nodes[1].strike - nodes[0].strike) : 25;

  return (
    <div style={{ background:'#04080f', color:'#c8d8e8', minHeight:'100vh', maxHeight:'100vh', display:'flex', flexDirection:'column', overflow:'hidden', fontFamily:"'JetBrains Mono','Courier New',monospace" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@400;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        ::-webkit-scrollbar{width:3px;background:#04080f}
        ::-webkit-scrollbar-thumb{background:#1a2535;border-radius:2px}
        .pulse{animation:pulse 1.8s ease-in-out infinite}
        @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.25}}
        .reshuffle{animation:flash 0.9s ease-out}
        @keyframes flash{0%{background:rgba(0,212,255,0.1)}100%{background:transparent}}
        .bar-fill{transition:width 0.6s ease}
        .nrow{transition:background 0.15s}
        .nrow:hover{background:rgba(255,255,255,0.025)!important}
        .panel{background:rgba(8,16,28,0.85);border:1px solid rgba(255,255,255,0.06);border-radius:3px}
        select.sel{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:#c8d8e8;border-radius:3px;padding:4px 8px;font-size:12px;font-family:'JetBrains Mono',monospace;outline:none;cursor:pointer}
        select.sel:focus{border-color:rgba(10,255,157,0.4)}
        button.btn{padding:4px 12px;border-radius:3px;font-size:11px;cursor:pointer;font-family:'Chakra Petch',sans-serif;font-weight:600;letter-spacing:0.08em;transition:all 0.2s;border:1px solid}
        button.btn:disabled{opacity:0.5;cursor:not-allowed}
      `}</style>

      {/* ─── TOP BAR ──────────────────────────────────────────── */}
      <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between', padding:'9px 16px', borderBottom:'1px solid rgba(255,255,255,0.06)', background:'rgba(0,0,0,0.5)', gap:12, flexShrink:0, flexWrap:'wrap' }}>

        {/* Left: logo + controls */}
        <div style={{ display:'flex', alignItems:'center', gap:12 }}>
          <span style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:15, fontWeight:700, letterSpacing:'0.12em', color:'#0aff9d' }}>HEATSEEKER™</span>
          <div style={{ width:1, height:18, background:'rgba(255,255,255,0.08)' }} />
          <select className="sel" value={symbol} onChange={e=>setSymbol(e.target.value)}>
            {SYMBOLS.map(s=><option key={s}>{s}</option>)}
          </select>
          <select className="sel" value={dte} onChange={e=>setDte(Number(e.target.value))}>
            {DTE_OPTS.map(d=><option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
          <button className="btn" onClick={startStream} disabled={loading}
            style={{ background:'rgba(10,255,157,0.1)', borderColor:'rgba(10,255,157,0.35)', color:'#0aff9d' }}>
            {loading ? 'LOADING...' : streaming ? '↺ RESTART' : '▶ START'}
          </button>
        </div>

        {/* Center: price */}
        <div style={{ display:'flex', alignItems:'baseline', gap:10 }}>
          <span style={{ fontSize:10, color:'#334', letterSpacing:'0.1em' }}>${symbol}</span>
          <span style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:22, fontWeight:700, color: spot ? '#eef4fc' : '#1a2535' }}>
            {spot?.toFixed(2) ?? '——.——'}
          </span>
          {spot && (
            <span style={{ fontSize:12, fontWeight:600, color: isUp ? '#0aff9d' : '#ff3a5c' }}>
              {isUp?'▲':'▼'} {Math.abs(spotChange).toFixed(2)}
            </span>
          )}
        </div>

        {/* Right: signals + live dot + logout */}
        <div style={{ display:'flex', alignItems:'center', gap:8 }}>
          {Object.entries(signals).map(([k,v]) => <Badge key={k} label={k} dir={v} />)}
          <div style={{ padding:'3px 10px', background: aligned?'rgba(10,255,157,0.05)':'rgba(255,200,0,0.07)', border:`1px solid ${aligned?'rgba(10,255,157,0.28)':'rgba(255,200,0,0.3)'}`, borderRadius:3, fontSize:11, color: aligned?'#0aff9d':'#ffc840' }}>
            {aligned ? '✓ ALIGNED' : '⚠ DIVERGE'}
          </div>
          <div style={{ display:'flex', alignItems:'center', gap:6, fontSize:10, color:'#334', marginLeft:4 }}>
            {streaming && <div className="pulse" style={{ width:6, height:6, borderRadius:'50%', background:'#0aff9d', boxShadow:'0 0 6px #0aff9d' }} />}
            {streaming ? lastUpdate?.toLocaleTimeString() : 'OFFLINE'}
          </div>
          <button className="btn" onClick={handleLogout}
            style={{ background:'transparent', borderColor:'rgba(255,58,92,0.2)', color:'rgba(255,58,92,0.5)' }}>
            LOGOUT
          </button>
        </div>
      </div>

      {/* ─── STATUS ──────────────────────────────────────────── */}
      <div style={{ padding:'4px 16px', borderBottom:'1px solid rgba(255,255,255,0.04)', fontSize:10, color:'#2a4a6a', background:'rgba(0,0,0,0.3)', flexShrink:0 }}>
        {status}
      </div>

      {/* ─── MAIN ────────────────────────────────────────────── */}
      <div style={{ display:'flex', flex:1, gap:8, padding:8, overflow:'hidden', minHeight:0 }}>

        {/* LEFT — GEX Heatmap */}
        <div className="panel" style={{ width:310, display:'flex', flexDirection:'column', overflow:'hidden', flexShrink:0 }}>
          <PHead title="GEX HEATMAP" right="γ EXPOSURE · $B" />

          <div style={{ display:'flex', gap:12, padding:'6px 14px', borderBottom:'1px solid rgba(255,255,255,0.04)', fontSize:10, flexShrink:0 }}>
            <span style={{ color:'#ffd700' }}>★ KING</span>
            <span style={{ color:'#00d4ff' }}>⬡ GATE</span>
            <span style={{ color:'#0aff9d' }}>+ GEX</span>
            <span style={{ color:'#ff3a5c' }}>− GEX</span>
            {reshuffling && <span className="pulse" style={{ color:'#00d4ff', marginLeft:'auto' }}>↻ RESHUFFLE</span>}
          </div>

          <div style={{ flex:1, overflowY:'auto' }}>
            {nodes.length === 0 ? (
              <div style={{ padding:'40px 16px', textAlign:'center', fontSize:11, color:'#1a2535' }}>
                {loading ? 'Loading...' : 'Press START to stream live GEX data'}
              </div>
            ) : (
              [...nodes].reverse().map(({ strike, gex }) => {
                const type   = nodeType(strike);
                const atPrice= spot && Math.abs(strike - spot) < stepSize / 2;
                const pct    = (Math.abs(gex) / maxGex) * 100;
                const isPos  = gex >= 0;
                return (
                  <div key={strike} className={`nrow ${reshuffling ? 'reshuffle' : ''}`} style={{
                    display:'flex', alignItems:'center', gap:8, padding:'3.5px 14px',
                    background: type==='king' ? 'rgba(255,215,0,0.04)' : type==='gate' ? 'rgba(0,212,255,0.03)' : 'transparent',
                    borderLeft: atPrice ? '2px solid rgba(255,255,255,0.35)' : '2px solid transparent',
                  }}>
                    <span style={{ width:12, fontSize:10, flexShrink:0 }}>
                      {type==='king' ? <span style={{ color:'#ffd700', textShadow:'0 0 8px #ffd700' }}>★</span>
                        : type==='gate' ? <span style={{ color:'#00d4ff', textShadow:'0 0 8px #00d4ff' }}>⬡</span>
                        : ' '}
                    </span>
                    <span style={{ fontSize:11, width:42, textAlign:'right', flexShrink:0,
                      color: type==='king'?'#ffd700' : type==='gate'?'#00d4ff' : atPrice?'#fff':'#3a5a7a',
                      fontWeight: type ? 600 : 400 }}>{strike}</span>
                    <div style={{ flex:1, height:12, background:'rgba(255,255,255,0.03)', borderRadius:2, overflow:'hidden', position:'relative' }}>
                      <div className="bar-fill" style={{
                        position:'absolute',
                        left: isPos ? '50%' : `calc(50% - ${pct/2}%)`,
                        width:`${pct/2}%`, height:'100%',
                        background: isPos ? `rgba(10,255,157,${0.2+pct/100*0.7})` : `rgba(255,58,92,${0.2+pct/100*0.7})`,
                      }} />
                      <div style={{ position:'absolute', left:'50%', top:0, width:1, height:'100%', background:'rgba(255,255,255,0.06)' }} />
                    </div>
                    <span style={{ fontSize:10, width:38, textAlign:'right', flexShrink:0, color:gexColor(gex) }}>
                      {gex>0?'+':''}{gex.toFixed(2)}
                    </span>
                  </div>
                );
              })
            )}
          </div>

          <div style={{ padding:'7px 14px', borderTop:'1px solid rgba(255,255,255,0.05)', display:'flex', justifyContent:'space-between', fontSize:10, flexShrink:0 }}>
            <span>KING <span style={{ color:'#ffd700' }}>{kingNode?.strike ?? '—'}</span></span>
            <span>GATES <span style={{ color:'#00d4ff' }}>{gatekeepers.map(g=>g.strike).join(' · ') || '—'}</span></span>
          </div>
        </div>

        {/* RIGHT — Charts */}
        <div style={{ flex:1, display:'flex', flexDirection:'column', gap:8, overflow:'hidden', minWidth:0 }}>

          {/* Price + node chart */}
          <div className="panel" style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden', minHeight:0 }}>
            <PHead title={`${symbol} · LIVE PRICE`}
              right={<span style={{ display:'flex', gap:16 }}>
                {kingNode && <span style={{ color:'#ffd700' }}>── KING {kingNode.strike}</span>}
                {gatekeepers.map(g=><span key={g.strike} style={{ color:'#00d4ff' }}>── GATE {g.strike}</span>)}
              </span>}
            />
            <div style={{ flex:1, padding:'6px 4px 4px 0', minHeight:0 }}>
              <ResponsiveContainer width="100%" height="100%">
                <ComposedChart data={priceHistory} margin={{ top:6, right:18, bottom:4, left:0 }}>
                  <defs>
                    <linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#0aff9d" stopOpacity={0.12}/>
                      <stop offset="100%" stopColor="#0aff9d" stopOpacity={0.01}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="2 4" stroke="rgba(255,255,255,0.025)" />
                  <XAxis dataKey="time" tick={false} axisLine={false} tickLine={false} />
                  <YAxis domain={[chartMin, chartMax]} tick={{ fontSize:10, fill:'#334', fontFamily:'JetBrains Mono' }} axisLine={false} tickLine={false} tickFormatter={v=>v.toFixed(0)} width={50} />
                  <Tooltip contentStyle={{ background:'#0a141f', border:'1px solid rgba(255,255,255,0.1)', borderRadius:4, fontSize:11, fontFamily:'JetBrains Mono' }} formatter={v=>[v.toFixed(2),'Price']} labelFormatter={()=>''} />
                  {kingNode && chartMin < kingNode.strike && kingNode.strike < chartMax && (
                    <ReferenceLine y={kingNode.strike} stroke="#ffd700" strokeDasharray="5 3" strokeWidth={1.5}
                      label={{ value:`KING ${kingNode.strike}`, fill:'#ffd700', fontSize:9, position:'insideTopRight', fontFamily:'JetBrains Mono' }} />
                  )}
                  {gatekeepers.map(g => chartMin < g.strike && g.strike < chartMax && (
                    <ReferenceLine key={g.strike} y={g.strike} stroke="#00d4ff" strokeDasharray="3 4" strokeWidth={1}
                      label={{ value:`GATE ${g.strike}`, fill:'#00d4ff', fontSize:9, position:'insideTopRight', fontFamily:'JetBrains Mono' }} />
                  ))}
                  {spot && <ReferenceLine y={spot} stroke="rgba(255,255,255,0.25)" strokeWidth={1}
                    label={{ value:spot.toFixed(1), fill:'rgba(255,255,255,0.5)', fontSize:9, position:'insideTopLeft', fontFamily:'JetBrains Mono' }} />}
                  <Area type="monotone" dataKey="price" stroke="#0aff9d" strokeWidth={1.5} fill="url(#ag)" dot={false} isAnimationActive={false} />
                </ComposedChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Vanna chart */}
          <div className="panel" style={{ height:165, display:'flex', flexDirection:'column', flexShrink:0 }}>
            <PHead title="VANNA EXPOSURE" right="δ/σ SENSITIVITY BY STRIKE" />
            <div style={{ flex:1, padding:'4px 4px 4px 0' }}>
              {nodes.length === 0
                ? <div style={{ height:'100%', display:'flex', alignItems:'center', justifyContent:'center', fontSize:10, color:'#1a2535' }}>Waiting for data...</div>
                : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={nodes} margin={{ top:4, right:18, bottom:4, left:0 }}>
                      <CartesianGrid strokeDasharray="2 4" stroke="rgba(255,255,255,0.025)" />
                      <XAxis dataKey="strike" tick={{ fontSize:9, fill:'#334', fontFamily:'JetBrains Mono' }} axisLine={false} tickLine={false} interval={Math.floor(nodes.length/8)} />
                      <YAxis tick={{ fontSize:9, fill:'#334', fontFamily:'JetBrains Mono' }} axisLine={false} tickLine={false} width={36} />
                      <Tooltip contentStyle={{ background:'#0a141f', border:'1px solid rgba(255,255,255,0.1)', borderRadius:4, fontSize:11, fontFamily:'JetBrains Mono' }} formatter={v=>[v.toFixed(3),'Vanna']} labelFormatter={l=>`Strike ${l}`} />
                      <ReferenceLine y={0} stroke="rgba(255,255,255,0.08)" />
                      {kingNode && <ReferenceLine x={kingNode.strike} stroke="rgba(255,215,0,0.25)" strokeDasharray="3 3" />}
                      <Bar dataKey="vanna" radius={[2,2,0,0]} isAnimationActive={false}>
                        {nodes.map((d,i) => {
                          const t = nodeType(d.strike);
                          return <Cell key={i} fill={
                            t==='king' ? (d.vanna>=0?'rgba(255,215,0,0.85)':'rgba(255,150,0,0.85)')
                              : t==='gate' ? (d.vanna>=0?'rgba(0,212,255,0.65)':'rgba(0,150,200,0.65)')
                              : (d.vanna>=0?'rgba(10,255,157,0.5)':'rgba(255,58,92,0.5)')
                          } />;
                        })}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                )
              }
            </div>
          </div>
        </div>
      </div>

      {/* ─── FOOTER ──────────────────────────────────────────── */}
      <div style={{ padding:'5px 16px', borderTop:'1px solid rgba(255,255,255,0.04)', display:'flex', gap:20, fontSize:10, color:'#1e2e40', background:'rgba(0,0,0,0.4)', flexShrink:0 }}>
        <span>TASTYTRADE API · DXLINK STREAMER</span>
        <span>GEX = OI × γ × SPOT × 100</span>
        <span>VANNA ≈ VEGA × |δ| / (SPOT × σ)</span>
        <span style={{ marginLeft:'auto' }}>⚠ NOT FINANCIAL ADVICE</span>
      </div>
    </div>
  );
}

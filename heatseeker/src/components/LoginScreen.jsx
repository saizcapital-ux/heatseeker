import { useState } from 'react';
import { login } from '../api.js';

export default function LoginScreen({ onLogin }) {
  const [u, setU] = useState('');
  const [p, setP] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function handleSubmit(e) {
    e.preventDefault();
    setError(''); setLoading(true);
    try { await login(u, p); onLogin(); }
    catch (err) { setError(err.message); }
    finally { setLoading(false); }
  }

  return (
    <div style={{ minHeight:'100vh', background:'#04080f', display:'flex', alignItems:'center', justifyContent:'center', fontFamily:"'JetBrains Mono','Courier New',monospace" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@600;700&family=JetBrains+Mono:wght@400;500&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        .hs-input{width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:3px;padding:10px 14px;color:#c8d8e8;font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;transition:border-color 0.2s}
        .hs-input:focus{border-color:rgba(10,255,157,0.5)}
        .hs-input::placeholder{color:#2a3a4a}
        .hs-btn{width:100%;padding:11px;background:rgba(10,255,157,0.1);border:1px solid rgba(10,255,157,0.3);border-radius:3px;color:#0aff9d;font-family:'Chakra Petch',sans-serif;font-size:13px;font-weight:600;letter-spacing:0.1em;cursor:pointer;transition:all 0.2s}
        .hs-btn:hover:not(:disabled){background:rgba(10,255,157,0.18);border-color:rgba(10,255,157,0.5)}
        .hs-btn:disabled{opacity:0.5;cursor:not-allowed}
      `}</style>

      <div style={{ width:360, background:'rgba(8,16,28,0.95)', border:'1px solid rgba(255,255,255,0.08)', borderRadius:4, padding:'36px 32px' }}>
        <div style={{ textAlign:'center', marginBottom:32 }}>
          <div style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:22, fontWeight:700, letterSpacing:'0.1em', color:'#0aff9d', marginBottom:6 }}>HEATSEEKER™</div>
          <div style={{ fontSize:10, color:'#2a3a4a', letterSpacing:'0.12em' }}>GEX · VANNA · DEALER FLOW</div>
        </div>

        <form onSubmit={handleSubmit}>
          <div style={{ display:'flex', flexDirection:'column', gap:14 }}>
            <div>
              <div style={{ fontSize:10, color:'#445', letterSpacing:'0.1em', marginBottom:6 }}>TASTYTRADE USERNAME</div>
              <input className="hs-input" type="text" placeholder="your@email.com" value={u} onChange={e=>setU(e.target.value)} autoComplete="username" required />
            </div>
            <div>
              <div style={{ fontSize:10, color:'#445', letterSpacing:'0.1em', marginBottom:6 }}>PASSWORD</div>
              <input className="hs-input" type="password" placeholder="••••••••" value={p} onChange={e=>setP(e.target.value)} autoComplete="current-password" required />
            </div>

            {error && (
              <div style={{ padding:'8px 12px', background:'rgba(255,58,92,0.08)', border:'1px solid rgba(255,58,92,0.25)', borderRadius:3, fontSize:11, color:'#ff3a5c' }}>
                {error}
              </div>
            )}

            <button className="hs-btn" type="submit" disabled={loading}>
              {loading ? 'AUTHENTICATING...' : 'CONNECT →'}
            </button>
          </div>
        </form>

        <div style={{ marginTop:20, padding:'10px 12px', background:'rgba(255,255,255,0.02)', border:'1px solid rgba(255,255,255,0.04)', borderRadius:3, fontSize:10, color:'#2a3a4a', lineHeight:1.7 }}>
          ⓘ Credentials are forwarded directly to Tastytrade's API and are never logged or stored.
        </div>
      </div>
    </div>
  );
}

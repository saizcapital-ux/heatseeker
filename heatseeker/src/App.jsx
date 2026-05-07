import { useState, useEffect } from 'react';
import { hasSession, startSession } from './api.js';
import Dashboard from './components/Dashboard.jsx';

export default function App() {
  const [ready, setReady]   = useState(hasSession());
  const [error, setError]   = useState('');
  const [loading, setLoading] = useState(!hasSession());

  useEffect(() => {
    if (ready) return;
    startSession()
      .then(() => setReady(true))
      .catch(err => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return (
    <div style={{ minHeight:'100vh', background:'#04080f', display:'flex', alignItems:'center', justifyContent:'center', fontFamily:"'JetBrains Mono',monospace", color:'#0aff9d', fontSize:13 }}>
      <div style={{ textAlign:'center' }}>
        <div style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:20, fontWeight:700, letterSpacing:'0.1em', marginBottom:12 }}>HEATSEEKER™</div>
        <div style={{ color:'#334' }}>Authenticating with Tastytrade...</div>
      </div>
    </div>
  );

  if (error) return (
    <div style={{ minHeight:'100vh', background:'#04080f', display:'flex', alignItems:'center', justifyContent:'center', fontFamily:"'JetBrains Mono',monospace" }}>
      <div style={{ width:400, padding:'32px', background:'rgba(8,16,28,0.95)', border:'1px solid rgba(255,58,92,0.3)', borderRadius:4 }}>
        <div style={{ fontFamily:"'Chakra Petch',sans-serif", fontSize:16, fontWeight:700, color:'#0aff9d', marginBottom:16 }}>HEATSEEKER™</div>
        <div style={{ fontSize:11, color:'#ff3a5c', marginBottom:12 }}>⚠ Authentication Error</div>
        <div style={{ fontSize:11, color:'#c8d8e8', lineHeight:1.7, marginBottom:20 }}>{error}</div>
        <div style={{ fontSize:10, color:'#334', lineHeight:1.8 }}>
          Make sure these Railway Variables are set:<br/>
          <span style={{ color:'#0aff9d' }}>TT_CLIENT_ID · TT_CLIENT_SECRET · TT_REFRESH_TOKEN</span>
        </div>
        <button
          onClick={() => { setError(''); setLoading(true); setReady(false);
            startSession().then(() => setReady(true)).catch(e => setError(e.message)).finally(() => setLoading(false)); }}
          style={{ marginTop:16, width:'100%', padding:'9px', background:'rgba(10,255,157,0.1)', border:'1px solid rgba(10,255,157,0.3)', borderRadius:3, color:'#0aff9d', fontFamily:"'Chakra Petch',sans-serif", fontSize:12, fontWeight:600, letterSpacing:'0.08em', cursor:'pointer' }}>
          RETRY
        </button>
      </div>
    </div>
  );

  return <Dashboard onLogout={() => { setReady(false); setLoading(true); startSession().then(() => setReady(true)).catch(e => setError(e.message)).finally(() => setLoading(false)); }} />;
}

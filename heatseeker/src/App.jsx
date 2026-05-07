import { useState } from 'react';
import { isLoggedIn } from './api.js';
import LoginScreen from './components/LoginScreen.jsx';
import Dashboard from './components/Dashboard.jsx';

export default function App() {
  const [auth, setAuth] = useState(isLoggedIn());
  return auth
    ? <Dashboard onLogout={() => setAuth(false)} />
    : <LoginScreen onLogin={() => setAuth(true)} />;
}

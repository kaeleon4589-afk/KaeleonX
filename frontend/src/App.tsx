import { useEffect, useState } from 'react';
import { api, session } from './lib/api';
import type { User } from './types';
import AuthPage from './pages/AuthPage';
import DashboardPage from './pages/DashboardPage';

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(Boolean(session.get()));

  useEffect(() => {
    if (!session.get()) return;
    api.me().then(setUser).catch(() => session.clear()).finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="app-loader"><div className="loader-ring"/><strong>KAELEON</strong><span>Conectando con el motor…</span></div>;
  if (!user) return <AuthPage onAuthenticated={setUser} />;
  return <DashboardPage user={user} onSignedOut={() => setUser(null)} />;
}

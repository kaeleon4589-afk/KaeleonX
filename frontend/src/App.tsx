import { useEffect, useState } from 'react';
import { api, session } from './lib/api';
import type { User } from './types';
import AuthPage from './pages/AuthPage';
import DashboardPage from './pages/DashboardPage';
import AdminPage from './pages/AdminPage';

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(Boolean(session.get()));
  const [isAdmin, setIsAdmin] = useState(false);
  const [page, setPage] = useState<'dashboard'|'admin'>(() => window.location.pathname.startsWith('/admin') ? 'admin' : 'dashboard');

  useEffect(() => {
    if (!session.get()) return;
    api.me().then(async u => { setUser(u); try { await api.adminMe(); setIsAdmin(true); } catch { setIsAdmin(false); } }).catch(() => session.clear()).finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="app-loader"><div className="loader-ring"/><strong>KAELEON</strong><span>Conectando con el motor…</span></div>;
  if (!user) return <AuthPage onAuthenticated={async u => { setUser(u); try { await api.adminMe(); setIsAdmin(true); } catch { setIsAdmin(false); } }} />;
  if (page === 'admin' && isAdmin) return <AdminPage user={user} onBack={() => { history.pushState({}, '', '/'); setPage('dashboard'); }} />;
  return <DashboardPage user={user} isAdmin={isAdmin} onOpenAdmin={() => { history.pushState({}, '', '/admin'); setPage('admin'); }} onSignedOut={() => { setUser(null); setIsAdmin(false); }} />;
}

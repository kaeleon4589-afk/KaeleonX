const ADMIN_VIEWS = {
  users: '/users',
  subscriptions: '/subscriptions',
  payments: '/payments',
  referrals: '/referrals',
  operations: '/operations',
  engine: '/engine',
  markets: '/markets',
  audit: '/audit',
  events: '/events'
};

async function loadAdminView(view, render) {
  try {
    const data = await adminFetch(ADMIN_VIEWS[view]);
    render(data);
  } catch (e) {
    const target = document.querySelector(`[data-admin-view="${view}"]`);
    if (target) target.textContent = e.message === 'ADMIN_ACCESS_DENIED'
      ? 'Acceso ADMIN denegado'
      : 'Datos no disponibles';
  }
}

function renderRows(target, rows, fields) {
  if (!target) return;
  if (!Array.isArray(rows) || rows.length === 0) {
    target.innerHTML = '<div class="empty">Sin datos</div>';
    return;
  }
  target.innerHTML = rows.slice(0,50).map(row =>
    '<div class="data-row">' +
    fields.map(f => `<span><b>${f.label}</b>${escapeHtml(row[f.key])}</span>`).join('') +
    '</div>'
  ).join('');
}

function escapeHtml(v) {
  return String(v ?? '—').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
}

const API_BASE='/admin';
const $=id=>document.getElementById(id);

async function adminFetch(path){
  const r=await fetch(API_BASE+path,{credentials:'include',headers:{'Accept':'application/json'}});
  if(r.status===401||r.status===403) throw new Error('ADMIN_ACCESS_DENIED');
  if(!r.ok) throw new Error('ADMIN_API_'+r.status);
  return r.json();
}

function setMetric(id,value){ if($(id)) $(id).textContent = value ?? '—'; }

async function loadAdminDashboard(){
  try{
    const d=await adminFetch('/dashboard');
    setMetric('users',d.users?.active ?? d.users_count);
    setMetric('live',d.live?.active ?? d.live_count);
    setMetric('payments',d.payments?.pending ?? d.payments_count);
    setMetric('engine',d.engine?.status ?? d.engine_status);
    $('system').textContent='ADMIN — CONECTADO';
    if(d.events && $('events')) $('events').textContent=JSON.stringify(d.events.slice(0,8),null,2);
  }catch(e){
    $('system').textContent=e.message==='ADMIN_ACCESS_DENIED'?'ACCESO ADMIN DENEGADO':'BACKEND ADMIN NO DISPONIBLE';
  }
}
loadAdminDashboard();

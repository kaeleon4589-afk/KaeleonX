import type { ReactNode } from 'react';
export default function StatCard({label,value,sub,tone='default',children}:{label:string;value:ReactNode;sub?:ReactNode;tone?:'default'|'green'|'red';children?:ReactNode}) {
  return <div className="stat-card"><div className="muted-label">{label}</div><div className={`stat-value ${tone}`}>{value}</div>{sub && <div className="stat-sub">{sub}</div>}{children}</div>;
}

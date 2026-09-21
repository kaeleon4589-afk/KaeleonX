export default function Brand({compact=false}:{compact?:boolean}) {
  return <div className={`brand ${compact?'brand-compact':''}`}>
    <div className="brand-mark"><span>K</span></div>
    {!compact && <div><div className="brand-name">KAELEON</div><div className="brand-sub">TRADING INTELIGENTE, RESULTADOS REALES</div></div>}
  </div>;
}

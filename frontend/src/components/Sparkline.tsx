export default function Sparkline({values, className=''}:{values:number[];className?:string}) {
  const vals = values.length > 1 ? values : [0,0];
  const min=Math.min(...vals), max=Math.max(...vals), span=max-min||1;
  const points=vals.map((v,i)=>`${(i/(vals.length-1))*100},${36-((v-min)/span)*30}`).join(' ');
  return <svg className={`sparkline ${className}`} viewBox="0 0 100 40" preserveAspectRatio="none"><polyline points={points} fill="none" vectorEffect="non-scaling-stroke"/></svg>;
}

import {useEffect,useState} from 'react'
import {createBoundaryReview,listBoundaryReviews,controlBoundaryReview,getBoundaryResult} from '../api/ocr'
import type {BoundaryReviewJob} from '../api/ocr'
import {getApiErrorMessage} from '../api/client'
import {hasPermission} from '../api/auth'
import StructurePreview from './StructurePreview'

export default function BoundaryReview({jobId,selectedPages}:{jobId:string;selectedPages:number[]}) {
 const [pages,setPages]=useState('');const [checked,setChecked]=useState(false);const [jobs,setJobs]=useState<BoundaryReviewJob[]>([])
 const [result,setResult]=useState<Awaited<ReturnType<typeof getBoundaryResult>>|null>(null);const [error,setError]=useState('');const [busy,setBusy]=useState(false)
 useEffect(()=>{let current=true;const refresh=async()=>{try{const r=await listBoundaryReviews(jobId);if(current)setJobs(r.items)}catch(e){if(current)setError(getApiErrorMessage(e))}};setResult(null);void refresh();const timer=setInterval(()=>void refresh(),3000);return()=>{current=false;clearInterval(timer)}},[jobId])
 const execute=async(fn:()=>Promise<unknown>)=>{setBusy(true);setError('');try{await fn();setJobs((await listBoundaryReviews(jobId)).items)}catch(e){setError(getApiErrorMessage(e))}finally{setBusy(false)}}
 return <details data-testid="boundary-review"><summary>疑难跨页复核 · 本地2/3页窗口，不改成功页或索引</summary>
 <p>先复用原已完成页面；仅所选连续2或3页同时送本地引擎。结果是独立快照，差异需审核后显式重新解析/重建；不调用embedding或云chat。</p>
 {error&&<div className="alert-error">{error}</div>}
 <input className="form-control" aria-label="跨页复核页码" placeholder="例如 2,3 或 2,3,4" value={pages} onChange={e=>{setPages(e.target.value);setChecked(false)}}/>
 <label><input type="checkbox" checked={checked} disabled={!hasPermission('ops:write')} onChange={e=>setChecked(e.target.checked)}/> 确认本地计算与独立复核，不自动替换原资料</label>
 <button className="btn btn-secondary" disabled={busy||!checked||!hasPermission('ops:write')} onClick={()=>void execute(async()=>{const p=pages.split(/[,，]/).map(n=>Number(n.trim()));if(p.length<2||p.length>3||p.some((n,i)=>!Number.isInteger(n)||!selectedPages.includes(n)||i>0&&n!==p[i-1]+1))throw new Error('必须是已完成范围内的连续2/3页');await createBoundaryReview(jobId,p);setChecked(false)})}>提交本地跨页复核</button>
 <table className="table"><thead><tr><th>页 / 状态</th><th>尝试 / 缓存</th><th>操作</th></tr></thead><tbody>{jobs.map(j=><tr key={j.id}><td>{j.pages.join(', ')} · {j.status}<br/>{j.error}</td><td>{j.attempts} / {j.cache_hit?'命中':'未命中'}</td><td>{j.result_available&&<button className="btn btn-secondary btn-sm" onClick={()=>void execute(async()=>setResult(await getBoundaryResult(jobId,j.id)))}>查看独立复核</button>}{['pending','running'].includes(j.status)&&<button className="btn btn-secondary btn-sm" disabled={!hasPermission('ops:write')} onClick={()=>void execute(()=>controlBoundaryReview(jobId,j.id,'cancel'))}>取消复核</button>}{['failed','cancelled'].includes(j.status)&&<button className="btn btn-secondary btn-sm" disabled={!hasPermission('ops:write')} onClick={()=>void execute(()=>controlBoundaryReview(jobId,j.id,'resume'))}>续跑复核</button>}</td></tr>)}</tbody></table>
 {result&&<section><p>{result.notice} · {result.engine_version} · 全局页 {result.global_pages.join(', ')}</p><StructurePreview plan={result.structure}/>{result.document.blocks.map(b=><details key={b.block_id}><summary>原页{b.page_no} · {b.block_id} · {b.block_type}</summary><pre className="ocr-text">{b.text}</pre></details>)}</section>}
 </details>
}

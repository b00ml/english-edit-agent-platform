import { useEffect, useState } from 'react'
import { hasPermission } from '../api/auth'
import { getApiErrorMessage, listKnowledgeDocuments, removeKnowledgeDocument, getKnowledgeStructure, reviewKnowledgeStructure } from '../api/client'
import type { KnowledgeDocumentSummary } from '../api/client'
import type { StructurePlan } from '../api/types'
import StructurePreview from './StructurePreview'

export default function KnowledgeDocuments({ refresh, onChanged }: {refresh: number; onChanged: () => void}) {
  const [items,setItems] = useState<KnowledgeDocumentSummary[]>([])
  const [page,setPage] = useState(1); const [total,setTotal] = useState(0)
  const [error,setError] = useState(''); const [busy,setBusy] = useState(false)
  const [structure,setStructure] = useState<StructurePlan|null>(null); const [active,setActive] = useState('')
  const [accepted,setAccepted] = useState<string[]>([]); const [rejected,setRejected] = useState<string[]>([]); const [checked,setChecked] = useState(false)
  const openStructure = async (id:string) => {setError('');setBusy(true);setChecked(false);try{const p=await getKnowledgeStructure(id);setActive(id);setStructure(p);setAccepted(p.decisions.accepted_edge_ids);setRejected(p.decisions.rejected_edge_ids)}catch(e){setError(getApiErrorMessage(e))}finally{setBusy(false)}}
  useEffect(() => { let current=true; listKnowledgeDocuments(page).then(r=>{if(current){setItems(r.items);setTotal(r.total)}}).catch(e=>{if(current)setError(getApiErrorMessage(e))}); return ()=>{current=false} },[page,refresh])
  return <div className="card"><h3 className="section-title">资料索引 · 版本与撤除</h3>
    <p>撤除只删除知识索引，保留OCR原文件和审核预览。重建从上方OCR任务打开审核；旧索引在新向量全部验证并提交前保留。</p>
    {error && <div className="alert-error">{error}</div>}
    <div className="table-wrap"><table className="table"><thead><tr><th>资料</th><th>状态 / 版本</th><th>leaf / parent</th><th>embedding</th><th>操作</th></tr></thead>
    <tbody>{items.map(d=><tr key={d.id}><td>{d.source_name}</td><td>{d.status==='partial_index'?'部分索引已删除':'已索引'} / v{d.index_revision}<br/>布局：{d.chunk_layout ?? 'legacy'}</td><td>{d.leaf_count} / {d.parent_count}</td><td>{d.embedding_models.join(', ')}</td><td><button className="btn btn-secondary btn-sm" disabled={busy} onClick={()=>void openStructure(d.id)}>结构预览 / 免费审核</button> <button className="btn btn-secondary btn-sm" disabled={busy||!hasPermission('ops:write')} onClick={async()=>{if(!window.confirm(`撤除「${d.source_name}」的整个知识索引？OCR资料会保留。`))return;setBusy(true);setError('');try{await removeKnowledgeDocument(d.id);onChanged()}catch(e){setError(getApiErrorMessage(e))}finally{setBusy(false)}}}>撤除资料索引</button></td></tr>)}</tbody></table></div>
    {structure && <section><h4>活动索引的结构审核 · v{structure.index_revision}</h4>
      {!structure.expansion_allowed && <p className="alert-error">索引不完整，结构扩展禁用；请先重建。</p>}
      <StructurePreview plan={structure} accepted={accepted} rejected={rejected} onChange={hasPermission('ops:write') ? (id,action)=>{setAccepted(v=>[...v.filter(k=>k!==id),...(action==='accept'?[id]:[])]);setRejected(v=>[...v.filter(k=>k!==id),...(action==='reject'?[id]:[])]);setChecked(false)} : undefined}/>
      <label><input type="checkbox" checked={checked} disabled={!hasPermission('ops:write')} onChange={e=>setChecked(e.target.checked)}/> 已核对结构关系及原来源（不是事实认证）</label>
      <button className="btn btn-secondary" disabled={busy||!checked||!structure.expansion_allowed||!hasPermission('ops:write')} onClick={async()=>{setBusy(true);setError('');try{const p=await reviewKnowledgeStructure(active,{review_signature:structure.review_signature!,expected_index_revision:structure.index_revision!,source_reviewed:checked,accepted_edge_ids:accepted,rejected_edge_ids:rejected});setStructure(p);setChecked(false)}catch(e){setError(getApiErrorMessage(e))}finally{setBusy(false)}}}>保存结构关系（不重建、不调用embedding）</button>
    </section>}
    <div className="filter-bar"><button className="btn btn-secondary btn-sm" disabled={page===1} onClick={()=>setPage(v=>v-1)}>上一页资料</button><span>{page}/{Math.max(1,Math.ceil(total/20))}</span><button className="btn btn-secondary btn-sm" disabled={page*20>=total} onClick={()=>setPage(v=>v+1)}>下一页资料</button></div>
  </div>
}

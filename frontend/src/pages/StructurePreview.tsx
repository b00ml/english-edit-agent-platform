import type { StructurePlan } from '../api/types'

export default function StructurePreview({plan, accepted = [], rejected = [], onChange}: {
  plan: StructurePlan; accepted?: string[]; rejected?: string[]
  onChange?: (id: string, action: 'auto' | 'accept' | 'reject') => void
}) {
  const blocks = new Map(plan.blocks.map(block => [block.block_id, block]))
  const label = (id: string) => {const block=blocks.get(id);return `${id} · ${block?.page_no == null ? '无物理页' : `第${block.page_no}页`} · ${block?.role ?? ''}`}
  return <details data-testid="structure-preview"><summary>文档级结构（免费，不调用模型） · {plan.counts.units}个知识单元 / {plan.counts.accepted_edges}条确认关系 / {plan.counts.proposed_edges}条待确认</summary>
    <p className="hint">页眉保留在原始来源中，不重置正文小节。结构关系不是事实认证；缺页、排除块、新章节及独立题目是屏障。</p>
    <details><summary>章节层级 / 知识单元</summary>
      <ul>{plan.sections.map(section => <li key={section.id}>{'—'.repeat(Math.min(section.level,6))} {section.title}</li>)}</ul>
      {plan.units.map(unit => <p key={unit.id}>{unit.section_path.join(' > ') || '章节未知'}：{unit.member_ids.join(', ')}</p>)}
    </details>
    <details><summary>布局家具与屏障 · {plan.counts.furniture} / {plan.barriers.length}</summary>
      <pre className="ocr-text">{JSON.stringify({furniture:plan.blocks.filter(block=>block.role==='furniture'),barriers:plan.barriers},null,2)}</pre>
    </details>
    {(plan.logical_tables?.length ?? 0)>0 && <details><summary>逻辑表格 / 跨页单元格（源网格不修改）</summary><pre className="ocr-text">{JSON.stringify(plan.logical_tables,null,2)}</pre><p>续表须先确认；每个边界单元格分别审核。只列数/表头相同不自动合并。对应选择在下方关系审核。</p></details>}
    {(plan.unresolved_references?.length ?? 0)>0 && <p className="alert-error">章节引用未定位：{plan.unresolved_references?.map(r=>`${r.target}（${r.reason}）`).join('；')}</p>}
    <div className="table-wrap"><table className="table"><thead><tr><th>来源关系</th><th>证据 / 状态</th><th>审核决定</th></tr></thead><tbody>
      {plan.edges.map(edge => <tr key={edge.id}><td>{label(edge.from)} → {label(edge.to)}<br/>{edge.relation}</td><td>{edge.evidence.join(', ')}<br/>{edge.state}</td><td>{onChange ? <select className="form-control" aria-label={`结构关系 ${edge.id}`} value={accepted.includes(edge.id)?'accept':rejected.includes(edge.id)?'reject':'auto'} onChange={event=>onChange(edge.id,event.target.value as 'auto'|'accept'|'reject')}><option value="auto">采用证据默认</option><option value="accept">已核对，确认关联</option><option value="reject">拒绝关联</option></select> : edge.state}</td></tr>)}
    </tbody></table></div>
    <p className="hint">{plan.version} · policy {plan.policy_hash.slice(0,12)} · 结构签名 {plan.signature.slice(0,12)}。只返回实际来源片段，不伪造跨页单一坐标。</p>
  </details>
}

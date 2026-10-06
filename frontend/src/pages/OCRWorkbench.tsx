import BoundaryReview from './BoundaryReview'
import StructurePreview from './StructurePreview'
import { useCallback, useEffect, useRef, useState } from 'react'
import { hasPermission } from '../api/auth'
import { getApiErrorMessage } from '../api/client'
import { approveOCR, cancelOCR, getOCRImage, getOCRJob, getOCRReview, importOCR, listOCRJobs, resumeOCR, uploadOCR } from '../api/ocr'
import type { OCRBlock, OCRJob, OCRReview } from '../api/ocr'

const LABELS: Record<string, string> = { pending: '排队', running: '解析中', retry: '等待重试', completed: '解析完成', failed: '失败', cancelled: '已取消', not_requested: '未入库', indexing: '真实向量化中', indexed: '已入库', needs_attention: '需核对后重试', stale: '索引已修改，需重建', removed: '索引已撤除' }
function parsePages(value: string): number[] | null {
  if (!value.trim()) return null
  const pages = value.split(/[,，]/).map(v => Number(v.trim()))
  if (pages.some(v => !Number.isInteger(v) || v < 1) || pages.some((v, i) => i > 0 && v <= pages[i - 1])) throw new Error('页码需为递增、不重复的正整数，逗号分隔')
  return pages
}
export default function OCRWorkbench({ onIndexed }: { onIndexed: () => void }) {
  const canWrite = hasPermission('ops:write')
  const [jobs, setJobs] = useState<OCRJob[]>([]); const [listPage, setListPage] = useState(1); const [total, setTotal] = useState(0)
  const [active, setActive] = useState<OCRJob | null>(null); const [review, setReview] = useState<OCRReview | null>(null)
  const [file, setFile] = useState<File | null>(null); const [paths, setPaths] = useState(''); const [pages, setPages] = useState('')
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [message, setMessage] = useState('')
  const [pageNo, setPageNo] = useState(1); const [imageURL, setImageURL] = useState(''); const [coordinateMatch, setCoordinateMatch] = useState(false)
  const [originalBlocks, setOriginalBlocks] = useState<OCRBlock[]>([]); const [chosenBlock, setChosenBlock] = useState<OCRBlock | null>(null); const [excluded, setExcluded] = useState<string[]>([])
  const [sourceChecked, setSourceChecked] = useState(false); const [warningsChecked, setWarningsChecked] = useState(false); const [paidChecked, setPaidChecked] = useState(false)
  const [chunkLayout,setChunkLayout]=useState<'legacy'|'structure'>('structure')
  const [rebuildChecked, setRebuildChecked] = useState(false)
  const [acceptedEdges,setAcceptedEdges] = useState<string[]>([]); const [rejectedEdges,setRejectedEdges] = useState<string[]>([])
  const [selectionChecked, setSelectionChecked] = useState(false); const [retryChecked, setRetryChecked] = useState(false)
  const [type, setType] = useState('教材'); const [points, setPoints] = useState(''); const [note, setNote] = useState('')
  const mounted = useRef(true); const indexedSeen = useRef(new Set<string>())
  const load = useCallback(async () => {
    try { const result = await listOCRJobs(listPage); if (mounted.current) { setJobs(result.items); setTotal(result.total) } }
    catch (e) { if (mounted.current) setError(getApiErrorMessage(e, 'OCR任务加载失败')) }
  }, [listPage])
  useEffect(() => { mounted.current = true; void load(); const timer = window.setInterval(() => void load(), 3000); return () => { mounted.current = false; window.clearInterval(timer) } }, [load])
  useEffect(() => {
    if (!active) return
    const controller = { cancelled: false }
    const update = async () => { try { const job = await getOCRJob(active.id); if (controller.cancelled) return; setActive(job); const indexKey = `${job.id}:${job.index_revision}`; if (job.index_status === 'indexed' && !indexedSeen.current.has(indexKey)) { indexedSeen.current.add(indexKey); onIndexed() } } catch (e) { if (!controller.cancelled) setError(getApiErrorMessage(e)) } }
    const timer = window.setInterval(() => void update(), 2500)
    return () => { controller.cancelled = true; window.clearInterval(timer) }
  }, [active?.id, onIndexed])
  useEffect(() => {
    setImageURL(''); setCoordinateMatch(false); setChosenBlock(null)
    if (!active) return
    const controller = new AbortController(); let url = ''
    getOCRImage(active.id, pageNo, controller.signal).then(result => { if (controller.signal.aborted) return; url = URL.createObjectURL(result.blob); setImageURL(url); setCoordinateMatch(result.match) }).catch(e => { if (!controller.signal.aborted) setError(getApiErrorMessage(e, '原页加载失败')) })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [active?.id, pageNo, review?.preview_hash])
  const select = async (job: OCRJob) => {
    setActive(job); setReview(null); setOriginalBlocks([]); setExcluded([]); setPageNo(job.selected_pages[0]); setSourceChecked(false); setWarningsChecked(false); setPaidChecked(false); setSelectionChecked(false); setRetryChecked(false); setRebuildChecked(false); setAcceptedEdges([]); setRejectedEdges([]); setError('')
    if (job.status === 'completed') { try {
      const original = await getOCRReview(job.id, [], [], [], job.review_settings?.chunk_layout); setOriginalBlocks(original.document.blocks)
      setChunkLayout(original.chunk_layout)
      const previous = job.review_settings?.excluded_block_ids ?? []
      const approved = job.review_settings?.accepted_edge_ids ?? []; const denied = job.review_settings?.rejected_edge_ids ?? []
      const result = previous.length || approved.length || denied.length ? await getOCRReview(job.id, previous, approved, denied, original.chunk_layout) : original
      setAcceptedEdges(approved); setRejectedEdges(denied)
      setReview(result); setExcluded(previous); setPoints((job.review_settings?.knowledge_points ?? []).join(', '))
      setType(job.review_settings?.source_type ?? '教材'); setNote(job.review_settings?.review_note ?? '')
    } catch (e) { setError(getApiErrorMessage(e)) } }
  }
  const execute = async (action: () => Promise<unknown>) => { setBusy(true); setError(''); try { await action(); await load() } catch (e) { setError(getApiErrorMessage(e)) } finally { setBusy(false) } }
  const submit = () => execute(async () => {
    const selected = parsePages(pages)
    if (file) { if (file.size > 128 * 1024 * 1024) throw new Error('后台OCR文件上限128MiB'); const job = await uploadOCR(file, selected); await select(job); setMessage('已提交后台解析；尚未调用embedding') }
    else { const result = await importOCR(paths.split(/\r?\n/).map(v => v.trim()).filter(Boolean), selected); if (result.errors.length) setError(result.errors.map(e => `${e.file}：${e.message}`).join('；')); if (result.accepted[0]) await select(result.accepted[0]); setMessage(`已提交${result.accepted.length}份资料`) }
  })
  const pageBlocks = originalBlocks.filter(block => block.page_no === pageNo && block.text.trim())
  const bbox = chosenBlock?.source_locator.bbox
  const exclusionsSynced = !!review && JSON.stringify([...excluded].sort()) === JSON.stringify([...(review.document.stats.excluded_block_ids ?? [])].sort())
  const layoutSynced=review?.chunk_layout === chunkLayout
  const canApprove = canWrite && active?.status === 'completed' && review && sourceChecked && warningsChecked && paidChecked && layoutSynced && exclusionsSynced && JSON.stringify([...acceptedEdges].sort())===JSON.stringify([...(review.structure?.decisions.accepted_edge_ids ?? [])].sort()) && JSON.stringify([...rejectedEdges].sort())===JSON.stringify([...(review.structure?.decisions.rejected_edge_ids ?? [])].sort()) && review.chunks.length > 0 && (!active.partial_document || selectionChecked) && (!['failed', 'needs_attention'].includes(active.index_status) || retryChecked) && !['pending', 'indexing'].includes(active.index_status) && (active.index_status !== 'indexed' || rebuildChecked) && (!active.index_present || rebuildChecked)
  return <div className="card" data-testid="ocr-workbench">
    <h3 className="section-title">扫描 PDF · 后台 OCR 与审核入库</h3>
    <p className="text-secondary">OCR不自动入库。先逐页核对原图、表格与告警，再明确授权真实embedding；仅选页不会冒充整书。默认最多500页。</p>
    {error && <div className="alert-error" role="alert">{error}</div>}{message && <div className="alert-success">{message}</div>}
    <div className="filter-bar">
      <div className="form-group"><label className="form-label" htmlFor="ocr-file">PDF文件（上限128MiB）</label><input id="ocr-file" className="form-control" type="file" accept=".pdf" onChange={e => setFile(e.target.files?.[0] ?? null)} /></div>
      <div className="form-group"><label className="form-label" htmlFor="ocr-pages">物理页码（空白=全文件）</label><input id="ocr-pages" className="form-control" placeholder="例如 2,16" value={pages} onChange={e => setPages(e.target.value)} /></div>
      <button className="btn btn-primary" disabled={!canWrite || busy || (!file && !paths.trim())} onClick={submit}>提交后台 OCR</button>
    </div>
    <details><summary>配置目录内批次导入（相对路径，每行一份）</summary><textarea aria-label="OCR本地资料路径" className="form-control" rows={3} value={paths} onChange={e => { setPaths(e.target.value); setFile(null) }} placeholder="一、句法篇/第二章 并列句.pdf" /></details>
    <div className="table-wrap"><table className="table data-table"><thead><tr><th>资料</th><th>解析状态</th><th>进度</th><th>缓存页</th><th>入库状态</th><th>操作</th></tr></thead><tbody>{jobs.map(job => <tr key={job.id}><td>{job.filename}</td><td>{LABELS[job.status] ?? job.status}</td><td>{job.completed_pages}/{job.total_pages}</td><td>{job.cached_pages}</td><td>{LABELS[job.index_status] ?? job.index_status}</td><td><button className="btn btn-secondary btn-sm" onClick={() => void select(job)}>查看 / 审核</button></td></tr>)}</tbody></table></div>
    <div className="filter-bar"><button className="btn btn-secondary btn-sm" disabled={listPage === 1} onClick={() => setListPage(v => v - 1)}>上一页任务</button><span>{listPage} / {Math.max(1, Math.ceil(total / 20))}</span><button className="btn btn-secondary btn-sm" disabled={listPage * 20 >= total} onClick={() => setListPage(v => v + 1)}>下一页任务</button></div>
    {active?.status==='completed' && <BoundaryReview jobId={active.id} selectedPages={active.selected_pages}/>}
    {active && <section className="ocr-review">
      <h4>{active.filename} · {LABELS[active.status]} · {active.completed_pages}/{active.total_pages}</h4>
      <div className="filter-bar">
        {!['completed', 'cancelled', 'failed'].includes(active.status) && <button className="btn btn-secondary" disabled={!canWrite || busy} onClick={() => void execute(async () => { setActive(await cancelOCR(active.id)) })}>取消 OCR</button>}
        {['cancelled', 'failed'].includes(active.status) && <button className="btn btn-secondary" disabled={!canWrite || busy} onClick={() => void execute(async () => { setActive(await resumeOCR(active.id)) })}>续跑 OCR</button>}
        {active.status === 'completed' && !review && <button className="btn btn-primary" onClick={() => void select(active)}>打开审核预览</button>}
        <label>原页<select aria-label="OCR原页" className="form-control" value={pageNo} onChange={e => setPageNo(Number(e.target.value))}>{active.selected_pages.map(n => <option key={n} value={n}>第 {n} 页</option>)}</select></label>
      </div>
      {(active.error_message || active.index_error) && <div className="alert-error">{active.error_message ?? active.index_error}</div>}
      <div className="ocr-review-grid">
        <div><p>原页图像 · {coordinateMatch ? '页面尺寸已匹配，定位框仍需核对' : '坐标未验证，不绘制定位框'}</p><div className="ocr-source-image">{imageURL ? <img src={imageURL} alt={`原PDF第${pageNo}页`} /> : <p>加载原页…</p>}{coordinateMatch && bbox?.length === 4 && <div className="ocr-bbox" style={{ left: `${bbox[0] * 100}%`, top: `${bbox[1] * 100}%`, width: `${(bbox[2] - bbox[0]) * 100}%`, height: `${(bbox[3] - bbox[1]) * 100}%` }} />}</div></div>
        <div className="ocr-blocks"><p>识别块（点击定位；可排除疑似错误块，不擅自改写原文）</p>{pageBlocks.map(block => <article className="ocr-block" key={block.block_id}><div className="filter-bar"><button className="btn btn-secondary btn-sm" onClick={() => setChosenBlock(block)}>{block.block_type} · {block.block_id}</button><label><input type="checkbox" checked={excluded.includes(block.block_id)} onChange={e => setExcluded(v => e.target.checked ? [...v, block.block_id] : v.filter(id => id !== block.block_id))} /> 排除此块</label></div>{block.meta.rows ? <div className="table-wrap"><table className="table data-table"><tbody>{block.meta.rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div> : <pre className="ocr-text">{block.text}</pre>}</article>)}</div>
      </div>
      {review && <>
        <details><summary>解析告警 · {review.document.warnings.length} 项（需核对）</summary><ul>{review.document.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>
        <label>索引切分布局<select aria-label="索引切分布局" className="form-control" value={chunkLayout} onChange={e=>{setChunkLayout(e.target.value as 'legacy'|'structure');setSourceChecked(false);setWarningsChecked(false);setPaidChecked(false)}}><option value="legacy">旧版页内切分</option><option value="structure">结构知识单元 / 页边界软化</option></select></label>
        <p>修改布局需点击更新切块预览后重新核对；只在显式入库/重建时生效，不自动重嵌入所有资料。跨页源段与纯导航排除会单独显示。</p>
        {review.structure && <StructurePreview plan={review.structure} accepted={acceptedEdges} rejected={rejectedEdges} onChange={canWrite ? (id,action)=>{setAcceptedEdges(v=>[...v.filter(k=>k!==id),...(action==='accept'?[id]:[])]);setRejectedEdges(v=>[...v.filter(k=>k!==id),...(action==='reject'?[id]:[])]);setSourceChecked(false);setWarningsChecked(false);setPaidChecked(false)} : undefined}/>}
        <button className="btn btn-secondary" disabled={busy} onClick={()=>{setAcceptedEdges([]);setRejectedEdges([]);setSourceChecked(false);setWarningsChecked(false);setPaidChecked(false)}}>重置关系选择</button>
        <button className="btn btn-secondary" disabled={busy} onClick={() => void execute(async () => { setReview(await getOCRReview(active.id, excluded, acceptedEdges, rejectedEdges, chunkLayout)); setSourceChecked(false); setWarningsChecked(false); setPaidChecked(false) })}>更新排除后的切块预览</button>
        <details><summary>源段诊断 · {review.diagnostics.layout ?? 'legacy'} / {review.diagnostics.source_coverage_scope ?? '原字符'}</summary><pre className="ocr-text">{JSON.stringify({excluded_navigation:review.diagnostics.excluded_navigation_blocks,signature:review.diagnostics.plan_signature,chunks:review.chunks},null,2)}</pre></details>
        <details><summary>切块预览 · {review.chunks.length} 个leaf / {review.parents?.length ?? 0} 个parent</summary>{review.chunks.slice(0, 10).map((chunk, i) => <pre className="ocr-text" key={i}>{chunk.embedding_content}</pre>)}{review.chunks.length > 10 && <p>仅展示前10块；原文全部识别块可按页查看。</p>}</details>
        <div className="filter-bar"><label>资料类型<select className="form-control" value={type} onChange={e => setType(e.target.value)}>{['教材','课标','真题','练习册','其他'].map(v => <option key={v}>{v}</option>)}</select></label><label>知识点（逗号分隔）<input aria-label="OCR知识点" className="form-control" list="knowledge-point-options" value={points} onChange={e => setPoints(e.target.value)} /></label></div>
        <textarea aria-label="OCR审核备注" className="form-control" value={note} maxLength={1000} onChange={e => setNote(e.target.value)} placeholder="记录疑似源错误、人工排除与核对结论；不会自动修改原文" />
        <div className="ocr-checks"><label><input type="checkbox" checked={sourceChecked} onChange={e => setSourceChecked(e.target.checked)} /> 已核对所选原页和识别内容</label><label><input type="checkbox" checked={warningsChecked} onChange={e => setWarningsChecked(e.target.checked)} /> 已核对表格、阅读顺序、词边界及解析告警</label><label><input type="checkbox" checked={paidChecked} onChange={e => setPaidChecked(e.target.checked)} /> 同意调用真实 embedding，费用以供应商为准</label>{active.partial_document && <label><input type="checkbox" checked={selectionChecked} onChange={e => setSelectionChecked(e.target.checked)} /> 仅索引所选页，不声明整书已处理</label>}{['failed', 'needs_attention'].includes(active.index_status) && <label><input type="checkbox" checked={retryChecked} onChange={e => setRetryChecked(e.target.checked)} /> 已核对上次Trace，授权重试（可能再次计费）</label>}</div>
        {active.index_present && <label><input type="checkbox" checked={rebuildChecked} onChange={e => setRebuildChecked(e.target.checked)} /> 显式重建当前索引（版本{active.index_revision}），会重新调用真实embedding；成功前保留旧索引</label>}
        <p>{review.embedding.model} · {review.embedding.dimension}维 · {review.embedding.text_count}个文本输入。{review.embedding.price_configured ? '配置金额仅为估算，不是供应商账单。' : '未配置该模型价格，不展示金额承诺。'}</p>
        <button className="btn btn-primary" disabled={busy || !canApprove} onClick={() => void execute(async () => { setActive(await approveOCR(active.id, { preview_hash: review.preview_hash, chunk_layout: chunkLayout, plan_hash: review.plan_hash, source_reviewed: sourceChecked, warnings_acknowledged: warningsChecked, paid_embedding_acknowledged: paidChecked, accept_selected_pages: selectionChecked, retry_authorized: retryChecked, source_type: type, knowledge_points: points.split(/[,，]/).map(v => v.trim()).filter(Boolean), review_note: note, excluded_block_ids: excluded, rebuild_index: rebuildChecked, expected_index_revision: active.index_revision, accepted_edge_ids: acceptedEdges, rejected_edge_ids: rejectedEdges })); setMessage('已提交审核后的真实向量化任务；正在后台执行') })}>{active.index_status === 'indexed' && !rebuildChecked ? '已入库' : rebuildChecked ? '确认审核并重建索引' : '确认审核并真实向量化入库'}</button>
      </>}
      {active.pages && <details><summary>页状态与尝试次数</summary>{active.pages.map(p => <p key={p.page_no}>第{p.page_no}页 · {LABELS[p.status]} · 尝试{p.attempts} · {p.cache_hit ? '缓存命中' : '实际处理'} {p.error_message}</p>)}</details>}
    </section>}
  </div>
}

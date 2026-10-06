import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { cancelGenerationTask, getApiErrorMessage, getTask, listTasks, listTemplates } from '../api/client'
import { hasPermission } from '../api/auth'
import type { GenerationTask, TaskStatus } from '../api/types'
import Icon from '../components/Icon'
import Modal from '../components/Modal'

const LABELS: Record<TaskStatus,string> = {pending:'待执行',dispatched:'已调度',running:'执行中',awaiting_review:'等待人工裁决',succeeded:'已完成',partially_succeeded:'部分成功',failed:'失败',cancelled:'已取消'}
const RUNNING = new Set<TaskStatus>(['pending','dispatched','running','awaiting_review'])
const CANCELLABLE = new Set<TaskStatus>(['pending','dispatched','running'])
function percent(value: number) {return Number.isFinite(value) ? Math.round(Math.min(1,Math.max(0,value))*100) : 0}
function elapsed(task: GenerationTask) {
  const duration = Math.max(0,Math.round((new Date(task.updated_at).getTime()-new Date(task.created_at).getTime())/1000))
  return Number.isFinite(duration) ? duration < 60 ? `${duration} 秒` : `${Math.floor(duration/60)} 分 ${duration%60} 秒` : '—'
}
function date(value: string) {const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString('zh-CN',{hour12:false})}
export default function TasksPage() {
  const [searchParams,setSearchParams] = useSearchParams()
  const selectedId = searchParams.get('task_id') ?? ''
  const [tasks,setTasks] = useState<GenerationTask[]>([])
  const [names,setNames] = useState<Record<string,string>>({})
  const [total,setTotal] = useState(0)
  const [page,setPage] = useState(1)
  const [loading,setLoading] = useState(false)
  const [initialized,setInitialized] = useState(false)
  const [error,setError] = useState('')
  const [notice,setNotice] = useState('')
  const [detail,setDetail] = useState<GenerationTask | null>(null)
  const [detailError,setDetailError] = useState('')
  const [filter,setFilter] = useState('')
  const [query,setQuery] = useState('')
  const [auto,setAuto] = useState(true)
  const [updated,setUpdated] = useState('')
  const [confirm,setConfirm] = useState<GenerationTask | null>(null)
  const [cancelling,setCancelling] = useState(false)
  const [cancelledRequests,setCancelledRequests] = useState<Set<string>>(new Set())
  const request = useRef(0)
  const inFlight = useRef(false)
  const currentSelected = useRef(selectedId)
  currentSelected.current = selectedId
  const load = useCallback(async (number: number,quiet = false) => {
    if (quiet && inFlight.current) return
    const seq = ++request.current;inFlight.current = true
    if (!quiet) setLoading(true)
    try {
      const response = await listTasks(number,20)
      if (seq !== request.current) return
      setTasks(response.items);setTotal(response.total);setUpdated(new Date().toLocaleTimeString('zh-CN',{hour12:false}));setError('');setInitialized(true)
      setDetail(previous => response.items.find(t => t.id === currentSelected.current) ?? previous)
      if (currentSelected.current && !response.items.some(t => t.id === currentSelected.current)) {
        const latest = await getTask(currentSelected.current)
        if (seq === request.current && latest.id === currentSelected.current) setDetail(latest)
      }
    } catch(e) {if (seq === request.current) {setError(getApiErrorMessage(e,'任务读取失败，请重试'));setInitialized(true)}}
    finally {if (seq === request.current) {setLoading(false);inFlight.current = false}}
  },[])
  useEffect(() => {void load(page);return () => {request.current++;inFlight.current = false}},[page,load])
  useEffect(() => {let alive=true; listTemplates().then(rows => {if(alive) setNames(Object.fromEntries(rows.map(t => [t.type_id,t.name])))}).catch(() => undefined);return () => {alive=false}},[])
  useEffect(() => {
    setDetail(null);setDetailError('')
    if (!selectedId) return
    let alive=true
    getTask(selectedId).then(task => {if(alive) setDetail(task)}).catch(e => {if(alive) setDetailError(getApiErrorMessage(e,'任务详情读取失败'))})
    return () => {alive=false}
  },[selectedId])
  const active = tasks.some(t => RUNNING.has(t.status)) || Boolean(detail && RUNNING.has(detail.status))
  useEffect(() => {
    if (!auto || !active || confirm) return
    const timer = setInterval(() => {if (document.visibilityState === 'visible') void load(page,true)},5000)
    return () => clearInterval(timer)
  },[auto,active,confirm,page,load])
  const pages = Math.max(1,Math.ceil(total/20))
  const rows = tasks.filter(t => (!filter || t.status === filter) && (!query || `${t.id} ${names[t.template_id] ?? t.template_id}`.toLowerCase().includes(query.toLowerCase())))
  const copy = async (id: string) => {try {await navigator.clipboard.writeText(id);setNotice('任务 ID 已复制')} catch {setNotice('浏览器未授权复制，请从详情中手动复制任务 ID')}}
  const open = (task: GenerationTask) => {setDetail(task);setSearchParams({task_id:task.id})}
  const close = () => {setSearchParams({});setDetail(null);setDetailError('')}
  const cancel = async () => {
    if (!confirm || cancelling) return
    setCancelling(true);setError('')
    try {const response = await cancelGenerationTask(confirm.id);setCancelledRequests(prev => new Set([...prev,confirm.id]));setNotice(response.message || '取消请求已提交，正在执行的调用不会立即中断');setConfirm(null);await load(page,true)}
    catch(e) {setError(getApiErrorMessage(e,'取消请求失败，请稍后重试'))}
    finally {setCancelling(false)}
  }
  return <div className="page tasks-page">
    <div className="page-header"><div><p className="page-eyebrow">PRODUCTION QUEUE</p><h2 className="page-title">生成任务</h2><p className="page-description">跟踪执行状态与进度。任务完成不代表内容已通过人工审核。</p></div><div className="header-actions"><button className="btn btn-ghost" disabled={loading} onClick={() => load(page)}><Icon name="refresh" className={loading ? 'spin-icon' : ''}/>刷新</button>{hasPermission('generate:create') && <Link className="btn btn-primary" to="/"><Icon name="create"/>新建任务</Link>}</div></div>
    {error && <div className="alert-error" role="alert">{error}</div>}{notice && <div className="feedback-note" role="status"><Icon name="info"/><span>{notice}</span><button className="icon-button" onClick={() => setNotice('')} aria-label="关闭提示"><Icon name="close" size={15}/></button></div>}
    <div className="card task-toolbar"><div className="task-filter"><label htmlFor="task-search">搜索当前页</label><div className="search-field"><Icon name="search"/><input id="task-search" className="form-control" placeholder="任务 ID 或题型" value={query} onChange={e => setQuery(e.target.value)}/></div></div><div className="task-filter"><label htmlFor="task-filter">状态 · 当前页</label><select id="task-filter" className="form-control" value={filter} onChange={e => setFilter(e.target.value)}><option value="">全部状态</option>{Object.entries(LABELS).map(([key,label]) => <option key={key} value={key}>{label}</option>)}</select></div><div className="task-refresh"><label><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)}/>活动任务自动刷新</label><small>{updated ? `更新于 ${updated}` : '正在读取'} · {auto && active ? '可见页面每 5 秒更新' : '已暂停／无活动任务'}</small></div></div>
    <div className="card table-card task-table-card" aria-busy={loading}>
      <div className="table-caption"><strong>执行记录</strong><span>{filter || query ? `当前页显示 ${rows.length} 条 · ` : ''}共 {total} 条任务</span></div>
      <div className="table-scroll"><table className="table"><thead><tr><th>任务 / 创建时间</th><th>题型</th><th>数量</th><th>执行状态</th><th>完成进度</th><th>累计时长</th><th>操作</th></tr></thead><tbody>
        {!initialized && loading ? Array.from({length:4},(_,i) => <tr key={i} aria-hidden="true"><td colSpan={7}><div className="skeleton-line"/></td></tr>) : rows.map(task => <tr key={task.id}>
          <td><div className="task-id"><button className="task-id-link mono" onClick={() => open(task)} title={task.id}>{task.id.slice(0,8)}…</button><button className="icon-button small-icon" aria-label={`复制任务 ID ${task.id}`} onClick={() => copy(task.id)}><Icon name="copy" size={14}/></button></div><small className="table-secondary">{date(task.created_at)}</small></td>
          <td>{names[task.template_id] ?? task.template_id}</td><td>{task.quantity} 项</td><td><span className={`status-tag status-${task.status}`}><span className="status-dot"/>{LABELS[task.status] ?? task.status}</span>{cancelledRequests.has(task.id) && CANCELLABLE.has(task.status) && <small className="table-secondary">取消请求处理中</small>}</td>
          <td><div className="task-progress"><div className="progress" role="progressbar" aria-label="任务完成进度" aria-valuenow={percent(task.progress)} aria-valuemin={0} aria-valuemax={100}><div className="progress-bar" style={{width:`${percent(task.progress)}%`}}/></div><span className="progress-text">{percent(task.progress)}%</span></div></td><td>{elapsed(task)}</td>
          <td><div className="table-actions"><button className="btn btn-ghost btn-sm" onClick={() => open(task)}>查看详情</button>{hasPermission('generate:cancel') && CANCELLABLE.has(task.status) && <button className="btn btn-ghost btn-sm" disabled={cancelledRequests.has(task.id)} onClick={() => setConfirm(task)}>取消任务</button>}</div></td>
        </tr>)}
      </tbody></table></div>
      {initialized && rows.length === 0 && <div className="empty-state"><Icon name="tasks" size={30}/><h3>{tasks.length ? '当前页没有匹配的任务' : error ? '暂时无法读取任务' : '还没有生成任务'}</h3><p>{tasks.length ? '调整状态或搜索条件；筛选仅作用于当前页。' : error ? '现有记录不会因读取失败消失，可以重新尝试。' : '从一个知识点和一份参考资料开始创建任务。'}</p>{tasks.length ? <button className="btn btn-ghost" onClick={() => {setQuery('');setFilter('')}}>清除筛选</button> : <Link className="btn btn-primary" to="/">开始生成</Link>}</div>}
      <div className="pagination"><span className="pagination-total">第 {page} / {pages} 页 · 共 {total} 条</span><button className="btn btn-ghost btn-sm" disabled={page<=1 || loading} onClick={() => setPage(page-1)}>上一页</button><button className="btn btn-ghost btn-sm" disabled={page>=pages || loading} onClick={() => setPage(page+1)}>下一页</button></div>
    </div>
    {selectedId && <Modal title="任务详情" onClose={close} footer={<><span className="muted">执行成功 ≠ 人工通过</span><button className="btn btn-ghost" onClick={close}>关闭</button></>}>
      {detailError ? <div className="alert-error" role="alert">{detailError}</div> : !detail ? <p role="status">正在加载任务详情…</p> : <><div className="detail-heading"><span className={`status-tag status-${detail.status}`}>{LABELS[detail.status]}</span><span>{names[detail.template_id] ?? detail.template_id} · {detail.quantity} 项</span></div><dl className="detail-list"><dt>完整任务 ID</dt><dd className="mono">{detail.id}</dd><dt>完成进度</dt><dd>{percent(detail.progress)}%</dd><dt>创建时间</dt><dd>{date(detail.created_at)}</dd><dt>最近更新</dt><dd>{date(detail.updated_at)}</dd><dt>累计时长</dt><dd>{elapsed(detail)}</dd><dt>生成参数</dt><dd><pre className="pre">{JSON.stringify(detail.params,null,2)}</pre></dd></dl>{detail.status==='awaiting_review' && <div className="inline-note"><Icon name="quality"/><span>有条目等待人工裁决，前往质检工作区处理。</span><Link className="text-link" to="/quality">前往质检</Link></div>}</>}
    </Modal>}
    {confirm && <Modal title="确认取消任务？" onClose={() => {if(!cancelling) setConfirm(null)}} busy={cancelling} footer={<><button className="btn btn-ghost" disabled={cancelling} onClick={() => setConfirm(null)}>继续执行</button><button className="btn btn-primary" disabled={cancelling} onClick={cancel}>{cancelling ? '提交取消请求…' : '确认取消'}</button></>}><p>取消任务 <span className="mono">{confirm.id}</span>。</p><div className="inline-note"><Icon name="info"/><span>这是合作式取消。已经发出的模型调用不会立即中断，已发生费用不会撤销；界面会等待后台更新最终状态。</span></div>{error && <div className="alert-error" role="alert">{error}</div>}</Modal>}
  </div>
}

// Schema-driven fields keep wire names; UI labels and layout improve readability only.
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { generate, getApiErrorMessage, listTemplates } from '../api/client'
import type { Template } from '../api/types'
import Icon from '../components/Icon'

interface PropertySchema {type?: string; title?: string; description?: string; enum?: string[]; minimum?: number; maximum?: number; default?: string | number}
interface FieldDef extends PropertySchema {name: string; type: string; required: boolean}
const LABELS: Record<string, string> = {knowledge_point:'知识点', difficulty:'难度', quantity:'批次数量', topic:'语篇主题'}
const HINTS: Record<string, string> = {knowledge_point:'与知识库中的知识点保持一致，例如：一般现在时、名词。', difficulty:'选择目标学情难度，由模板配置决定生成要求。', quantity:'每个执行项生成一题或一篇语篇；内部子题数与批次数量不同。', topic:'为语篇提供主题方向，不替代知识点与参考资料。'}
function parseFields(template: Template): FieldDef[] {
  const schema = template.input_schema as {properties?: Record<string, PropertySchema>; required?: string[]}
  const required = new Set(schema.required ?? [])
  const priority: Record<string, number> = {knowledge_point:0, topic:1, difficulty:2, quantity:3}
  return Object.entries(schema.properties ?? {}).map(([name, def]) => ({...def,name,type:def.type ?? 'string',required:required.has(name)})).sort((a,b) => (priority[a.name] ?? 10) - (priority[b.name] ?? 10))
}
function defaultValue(field: FieldDef): string | number {
  if (field.default !== undefined) return field.default
  if (field.enum?.length) return field.enum[0]
  return field.type === 'integer' || field.type === 'number' ? field.minimum ?? 1 : ''
}

export default function GeneratePage() {
  const navigate = useNavigate()
  const [templates,setTemplates] = useState<Template[]>([])
  const [templateId,setTemplateId] = useState('')
  const [values,setValues] = useState<Record<string,string | number>>({})
  const [loading,setLoading] = useState(false)
  const [fetching,setFetching] = useState(true)
  const [error,setError] = useState('')
  const [invalid,setInvalid] = useState<Record<string,string>>({})
  const submitting = useRef(false)
  const template = templates.find(t => t.type_id === templateId)
  const fields = useMemo(() => template ? parseFields(template) : [], [template])
  const count = Number(values.quantity ?? 1)
  const loadTemplates = () => {
    setFetching(true); setError('')
    listTemplates().then(list => {const enabled = list.filter(t => t.status !== 'disabled'); setTemplates(enabled); setTemplateId(id => enabled.some(t => t.type_id === id) ? id : enabled[0]?.type_id ?? '')}).catch(e => setError(getApiErrorMessage(e,'模板加载失败，请重试'))).finally(() => setFetching(false))
  }
  useEffect(loadTemplates, [])
  useEffect(() => {setValues(Object.fromEntries(fields.map(f => [f.name,defaultValue(f)]))); setInvalid({})}, [fields])
  const setField = (name: string, value: string | number) => {setValues(prev => ({...prev,[name]:value})); setInvalid(prev => ({...prev,[name]:''}))}
  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (submitting.current) return
    const problems: Record<string,string> = {}
    for (const f of fields) {
      const value = values[f.name]
      if (f.required && !String(value ?? '').trim()) problems[f.name] = `请填写${f.title ?? LABELS[f.name] ?? f.name}`
      if ((f.type === 'integer' || f.type === 'number') && String(value ?? '').trim()) {
        const number = Number(value)
        if (!Number.isFinite(number) || (f.type === 'integer' && !Number.isInteger(number))) problems[f.name] = '请输入有效数字，整数参数不可填写小数'
        else if ((f.minimum !== undefined && number < f.minimum) || (f.maximum !== undefined && number > f.maximum)) problems[f.name] = `请输入${f.minimum ?? '不限'}～${f.maximum ?? '不限'}范围内的数值`
      }
    }
    setInvalid(problems)
    if (Object.keys(problems).length) {document.getElementById(`gen-${Object.keys(problems)[0]}`)?.focus(); return}
    if (!templateId) {setError('请选择可用题型'); return}
    setError(''); submitting.current = true; setLoading(true)
    const params: Record<string, unknown> = {}
    fields.forEach(f => {const v = values[f.name]; params[f.name] = f.type === 'integer' || f.type === 'number' ? Number(v) : v})
    const quantity = Number(params.quantity ?? 1)
    if ('quantity' in params) params.quantity = 1
    try {const result = await generate({template_id:templateId,quantity,params}); navigate(`/tasks?task_id=${result.task_id}`)}
    catch(e) {setError(getApiErrorMessage(e,'任务提交失败；请核对参数或稍后重试'))}
    finally {setLoading(false); submitting.current = false}
  }
  return <div className="page generate-page">
    <div className="page-header"><div><p className="page-eyebrow">CONTENT PRODUCTION</p><h2 className="page-title">生成内容</h2><p className="page-description">从知识点开始，生成有资料依据、可人工复核的英语题目。</p></div><Link to="/tasks" className="btn btn-ghost"><Icon name="tasks"/>查看任务</Link></div>
    {error && <div className="alert-error" role="alert">{error}{fetching ? null : templates.length === 0 && <button className="btn btn-ghost" onClick={loadTemplates}>重新加载</button>}</div>}
    <div className="generation-layout">
      <div className="card generation-card">
        <div className="section-heading"><span className="section-number">01</span><div><h3>选择题型</h3><p>每次任务使用一个模板，参数由模板自动提供。</p></div></div>
        <div className="template-options" aria-label="题型模板">{fetching ? <p role="status" className="muted">正在加载可用模板…</p> : templates.length === 0 ? <p className="muted">暂无可用题型，请联系管理员检查模板配置。</p> : templates.map(t => <button key={t.type_id} type="button" className={`template-option ${templateId === t.type_id ? 'selected' : ''}`} aria-pressed={templateId === t.type_id} disabled={loading} onClick={() => setTemplateId(t.type_id)}><Icon name="create" size={20}/><strong>{t.name}</strong><span>模板 v{t.version}</span></button>)}</div>
        <form noValidate onSubmit={handleSubmit} aria-busy={loading}>
          <div className="section-heading"><span className="section-number">02</span><div><h3>设置生成参数</h3><p>前端便捷校验之外，服务端会按模板再次检查。</p></div></div>
          <div className="generation-fields">{fields.map(f => <div className={`form-group ${f.type === 'string' && !f.enum ? 'field-wide' : ''}`} key={f.name}>
            <label htmlFor={`gen-${f.name}`} className="form-label">{f.title ?? LABELS[f.name] ?? f.name}{f.required && <span className="required-mark" aria-hidden="true"> *</span>}</label>
            {f.enum?.length ? <select id={`gen-${f.name}`} className="form-control" value={String(values[f.name] ?? '')} onChange={e => setField(f.name,e.target.value)} disabled={loading} aria-required={f.required} aria-invalid={Boolean(invalid[f.name])} aria-describedby={`help-${f.name}`}>{f.enum.map(v => <option key={v} value={v}>{v}</option>)}</select> : <input id={`gen-${f.name}`} className="form-control" type={f.type === 'integer' || f.type === 'number' ? 'number' : 'text'} min={f.minimum} max={f.maximum} step={f.type === 'integer' ? 1 : f.type === 'number' ? 'any' : undefined} value={values[f.name] ?? ''} onChange={e => setField(f.name,e.target.value)} disabled={loading} aria-required={f.required} aria-invalid={Boolean(invalid[f.name])} aria-describedby={`help-${f.name}`} placeholder={f.name === 'knowledge_point' ? '填写本次要覆盖的知识点' : `填写${f.title ?? LABELS[f.name] ?? f.name}`}/>}<p className={invalid[f.name] ? 'field-error' : 'field-help'} id={`help-${f.name}`}>{invalid[f.name] || f.description || HINTS[f.name] || `参数标识：${f.name}`}</p>
          </div>)}</div>
          <div className="generation-submit"><span className="muted">{loading ? '正在创建任务，请勿重复提交' : '提交后可在任务页查看执行进度'}</span><button className="btn btn-primary" type="submit" disabled={loading || fetching || !templateId}>{loading ? <><span className="spinner"/>提交中…</> : <><Icon name="create"/>创建生成任务<Icon name="arrow" size={16}/></>}</button></div>
        </form>
      </div>
      <aside className="generation-aside" aria-label="任务摘要与使用边界"><div className="card summary-card"><p className="page-eyebrow">本次任务</p><h3>{template?.name ?? '请选择题型'}</h3><dl className="summary-list"><dt>批次数量</dt><dd>{Number.isFinite(count) && count > 0 ? count : '—'} 个执行项</dd><dt>难度</dt><dd>{String(values.difficulty ?? '由模板决定')}</dd><dt>知识点</dt><dd>{String(values.knowledge_point || '待填写')}</dd></dl><div className="inline-note"><Icon name="info"/><span>每项独立生成一题或一篇语篇。批次数量不等于完形空数／阅读小题数。</span></div></div><div className="workflow-guide"><p className="section-title">生成后，还需要两步</p><div><span>1</span><p><strong>人工核对</strong><small>检查来源、正确答案与干扰项，自动高分不是通过证明。</small></p></div><div><span>2</span><p><strong>通过后发布</strong><small>内容入库仍待审核，不会自动发布到外部平台。</small></p></div><Link to="/knowledge" className="text-link">先检查知识资料 <Icon name="arrow" size={14}/></Link></div></aside>
    </div>
  </div>
}

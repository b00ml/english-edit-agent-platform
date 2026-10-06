import { useEffect, useState } from 'react'
import { hasPermission } from '../api/auth'
import { deleteModelProvider, getApiErrorMessage, getModelCapabilities, getModelProfiles, getModelProviders, getModelRoutes, probeModelProvider, saveModelProfile, saveModelProvider, saveModelRoute } from '../api/client'
import type { EditableModelProfile, ModelProviderConfig, ModelTemplateRoute } from '../api/client'

const emptyProvider = {id: '', name: '', base_url: '', api_key: '', status: 'enabled' as 'enabled' | 'disabled'}
const emptyProfile: EditableModelProfile = {name: '', provider: 'environment', provider_id: null, model_name: '', cost_tier: 'standard', is_default: false, status: 'enabled', max_fallbacks: 1, budget_per_task: null, tenant_id: null}

export default function ModelSettingsPage() {
  const [providers, setProviders] = useState<ModelProviderConfig[]>([])
  const [profiles, setProfiles] = useState<EditableModelProfile[]>([])
  const [routes, setRoutes] = useState<ModelTemplateRoute[]>([])
  const [cap, setCap] = useState<{credential_storage_ready: boolean; legacy_base_url: string; legacy_model: string} | null>(null)
  const [provider, setProvider] = useState({...emptyProvider})
  const [profile, setProfile] = useState<EditableModelProfile>({...emptyProfile})
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const allowed = hasPermission('model:manage')
  async function load() {
    const [a,b,c,d] = await Promise.all([getModelProviders(),getModelProfiles(),getModelRoutes(),getModelCapabilities()])
    setProviders(a); setProfiles(b); setRoutes(c); setCap(d)
  }
  useEffect(() => {if (allowed) load().catch(e => setError(getApiErrorMessage(e, '模型配置读取失败')))}, [allowed])
  async function action(fn: () => Promise<void>) {
    setBusy(true); setError(''); setMessage('')
    try {await fn(); await load()} catch (e) {setError(getApiErrorMessage(e, '模型配置操作失败'))} finally {setBusy(false)}
  }
  if (!allowed) return <div className="page model-settings-page"><div className="alert-error">模型设置仅管理员可操作。</div></div>
  return <div className="page model-settings-page">
    <div className="page-header"><h2 className="page-title">模型设置</h2><button className="btn btn-primary" disabled={busy} onClick={() => action(async () => {await load()})}>刷新</button></div>
    {error && <div className="alert-error" role="alert">{error}</div>}
    {message && <div className="card form-card model-settings-card" role="status">{message}</div>}
    <div className="card form-card model-settings-card"><h3 className="section-title">配置范围与生效方式</h3><p>支持 OpenAI 兼容的聊天接口。先配置 Provider 地址与密钥，再配置模型档案，最后按题型选择生成和 Judge 档案。未绑定 Provider 的旧档案继续使用环境变量，留空路由恢复题型默认；不会自动替换当前模型。</p><p>配置保存后供后续调用使用；已有完成内容不重跑，正在执行的后续节点可能使用新配置，建议活动任务结束后调整。/models 探测不生成内容，也不证明 JSON 输出、答案质量或服务账单。</p><p>API Key 仅写入加密存储，读取不会回显；编辑时留空保留原密钥。仅管理员可设置任意 HTTP(S)兼容端点，包括本机服务；本页不是支持公网匿名配置的代理。</p><p>当前环境默认：{cap?.legacy_model ?? '读取中'} · {cap?.legacy_base_url}</p>{cap && !cap.credential_storage_ready && <p className="alert-error">尚未配置独立 PROVIDER_SECRET_KEY，密钥写入不可用。旧环境模型仍可使用。</p>}</div>
    <div className="card form-card model-settings-card"><h3 className="section-title">Provider</h3>
      <table className="table"><thead><tr><th>名称</th><th>Base URL</th><th>密钥</th><th>状态</th><th>操作</th></tr></thead><tbody>{providers.map(p => <tr key={p.id}><td>{p.name}</td><td>{p.base_url}</td><td>{p.has_api_key ? '已配置（不回显）' : '未配置'}</td><td>{p.status === 'enabled' ? '启用' : '禁用'}</td><td><button className="btn btn-ghost" disabled={busy} onClick={() => setProvider({id:p.id,name:p.name,base_url:p.base_url,status:p.status,api_key:''})}>编辑</button><button className="btn btn-ghost" disabled={busy} onClick={() => action(async () => {const result = await probeModelProvider(p.id); setMessage(`模型列表探测成功：${result.models.join('、') || '空列表'}；未验证生成质量。`)})}>探测模型列表</button><button className="btn btn-ghost" disabled={busy} onClick={() => {if (window.confirm('删除此 Provider？被模型引用时服务端会拒绝。')) action(async () => {await deleteModelProvider(p.id); setMessage('Provider 已删除')})}}>删除</button></td></tr>)}</tbody></table>
      <form onSubmit={e => {e.preventDefault(); action(async () => {await saveModelProvider({id:provider.id || undefined,name:provider.name,base_url:provider.base_url,status:provider.status,...(provider.api_key ? {api_key:provider.api_key} : {})}); setProvider({...emptyProvider}); setMessage('Provider 已保存，密钥不会回显')})}}>
        <div className="form-group"><label className="form-label" htmlFor="provider-name">Provider 名称</label><input id="provider-name" className="form-control" required maxLength={64} value={provider.name} onChange={e => setProvider({...provider,name:e.target.value})}/></div>
        <div className="form-group"><label className="form-label" htmlFor="provider-base">Base URL（含服务要求的 /v1）</label><input id="provider-base" className="form-control" type="url" required value={provider.base_url} onChange={e => setProvider({...provider,base_url:e.target.value})}/></div>
        <div className="form-group"><label className="form-label" htmlFor="provider-key">API Key（编辑留空保留）</label><input id="provider-key" className="form-control" type="password" autoComplete="new-password" required={!provider.id} value={provider.api_key} onChange={e => setProvider({...provider,api_key:e.target.value})}/></div>
        <div className="form-group"><label className="form-label" htmlFor="provider-status">Provider 状态</label><select id="provider-status" className="form-control" value={provider.status} onChange={e => setProvider({...provider,status:e.target.value as 'enabled' | 'disabled'})}><option value="enabled">启用</option><option value="disabled">禁用</option></select></div>
        <div className="form-actions"><button className="btn btn-primary" disabled={busy || !cap?.credential_storage_ready}>保存 Provider</button><button type="button" className="btn btn-ghost" onClick={() => setProvider({...emptyProvider})}>新建 / 清空</button></div>
      </form>
    </div>
    <div className="card form-card model-settings-card"><h3 className="section-title">模型档案</h3><p>lite / standard / high 是路由档案名，不代表必然是不同模型。自定义模型的成本仍按后端价目表估算，不自动抓取供应商价格；Embedding / OCR 当前仍由原环境配置管理。设置默认档案用于现有生成降级路径；Judge 可独立选择档案。Provider 禁用时不能使用其密钥静默回退到环境端点。</p>
      <table className="table"><thead><tr><th>档案名</th><th>Provider</th><th>模型标识</th><th>默认</th><th>状态</th><th>操作</th></tr></thead><tbody>{profiles.map(p => <tr key={p.name}><td>{p.name}</td><td>{p.provider_id ? providers.find(x => x.id === p.provider_id)?.name ?? 'Provider 不可用' : '环境变量'}</td><td>{p.model_name}</td><td>{p.is_default ? '是' : '否'}</td><td>{p.status}</td><td><button className="btn btn-ghost" disabled={busy} onClick={() => setProfile({...p})}>编辑档案</button></td></tr>)}</tbody></table>
      <form onSubmit={e => {e.preventDefault(); action(async () => {await saveModelProfile(profile); setProfile({...emptyProfile}); setMessage('模型档案已保存；请核对题型路由')})}}>
        <div className="form-group"><label className="form-label" htmlFor="profile-name">档案名（更新按名称匹配）</label><input id="profile-name" className="form-control" required maxLength={64} value={profile.name} onChange={e => setProfile({...profile,name:e.target.value})}/></div>
        <div className="form-group"><label className="form-label" htmlFor="profile-provider">绑定 Provider</label><select id="profile-provider" className="form-control" value={profile.provider_id ?? ''} onChange={e => setProfile({...profile,provider_id:e.target.value || null,provider:providers.find(p => p.id === e.target.value)?.name ?? 'environment'})}><option value="">使用环境变量（兼容旧档案）</option>{providers.filter(p => p.status === 'enabled').map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></div>
        <div className="form-group"><label className="form-label" htmlFor="profile-model">模型标识（服务端原始名称）</label><input id="profile-model" className="form-control" required maxLength={128} value={profile.model_name} onChange={e => setProfile({...profile,model_name:e.target.value})}/></div>
        <div className="form-group"><label className="form-label" htmlFor="profile-status">档案状态</label><select id="profile-status" className="form-control" value={profile.status} onChange={e => setProfile({...profile,status:e.target.value as 'enabled' | 'disabled'})}><option value="enabled">启用</option><option value="disabled">禁用</option></select></div>
        <div className="form-group"><label><input type="checkbox" checked={profile.is_default} onChange={e => setProfile({...profile,is_default:e.target.checked})}/> 作为默认生成降级档案</label></div>
        <div className="form-group"><label className="form-label" htmlFor="profile-fallback">最大降级次数</label><input id="profile-fallback" className="form-control" type="number" min={0} max={20} value={profile.max_fallbacks} onChange={e => setProfile({...profile,max_fallbacks:Number(e.target.value)})}/></div>
        <div className="form-actions"><button className="btn btn-primary" disabled={busy}>保存模型档案</button><button type="button" className="btn btn-ghost" onClick={() => setProfile({...emptyProfile})}>新建 / 清空</button></div>
      </form>
    </div>
    <div className="card form-card model-settings-card"><h3 className="section-title">按题型配置生成 / Judge</h3><p>“沿用模板 / 环境默认”不覆盖旧设置；生成覆盖会取代该题型的默认与难度分档，留空恢复 YAML 分档。Judge 留空沿用模板或环境 Judge。</p>
      <table className="table"><thead><tr><th>题型</th><th>生成档案</th><th>Judge 档案</th><th>操作</th></tr></thead><tbody>{routes.map((r,i) => <tr key={r.template_id}><td>{r.name ?? r.template_id}</td>{(['generation_profile','judge_profile'] as const).map(field => <td key={field}><select aria-label={`${r.template_id}-${field}`} className="form-control" value={r[field] ?? ''} onChange={e => setRoutes(routes.map((row,j) => j === i ? {...row,[field]:e.target.value || null} : row))}><option value="">沿用模板 / 环境默认</option>{profiles.filter(p => p.status === 'enabled' && !p.tenant_id).map(p => <option key={p.name} value={p.name}>{p.name} · {p.model_name}</option>)}</select></td>)}<td><button className="btn btn-primary" disabled={busy} onClick={() => action(async () => {await saveModelRoute(r); setMessage(`${r.name ?? r.template_id} 路由已保存`)})}>保存路由</button></td></tr>)}</tbody></table>
    </div>
  </div>
}

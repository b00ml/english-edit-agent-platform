import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { getApiErrorMessage, login } from '../api/client'
import Icon from '../components/Icon'

export default function LoginPage() {
  const navigate = useNavigate()
  const [username,setUsername] = useState('')
  const [password,setPassword] = useState('')
  const [visible,setVisible] = useState(false)
  const [error,setError] = useState('')
  const [loading,setLoading] = useState(false)
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (loading) return
    if (!username.trim() || !password) {setError('请输入用户名和密码'); return}
    setError('');setLoading(true)
    try {await login(username.trim(),password); navigate('/',{replace:true})}
    catch(e) {setError(getApiErrorMessage(e,'登录失败，请检查账号或稍后重试'))}
    finally {setLoading(false)}
  }
  return <div className="login-page"><div className="login-layout">
    <section className="login-intro"><div className="login-wordmark"><span className="brand-logo">E</span><span>英语内容工作台</span></div><p className="page-eyebrow">ENGLISH CONTENT STUDIO</p><h1>让教研内容生产，<br/>更有依据与秩序。</h1><p>从知识资料到题目生成，再到人工核验与入库发布。把每一步的来源、质量和过程留在工作台。</p><div className="login-stages"><span>资料入库</span><Icon name="arrow" size={14}/><span>内容生成</span><Icon name="arrow" size={14}/><span>人工复核</span></div><div className="login-boundary"><Icon name="quality"/><span>自动评分用于筛选，正式使用前仍需人工核对。</span></div></section>
    <form className="card login-card" noValidate onSubmit={submit} aria-busy={loading}><p className="page-eyebrow">欢迎回来</p><h2>登录工作台</h2><p className="login-sub">使用已有账号继续你的教研工作。</p>
      {error && <div className="alert-error" role="alert">{error}</div>}
      <div className="form-group"><label className="form-label" htmlFor="login-username">用户名</label><input className="form-control" id="login-username" value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" autoFocus required disabled={loading} placeholder="请输入用户名"/></div>
      <div className="form-group"><label className="form-label" htmlFor="login-password">密码</label><div className="password-field"><input className="form-control" id="login-password" value={password} onChange={e => setPassword(e.target.value)} type={visible ? 'text' : 'password'} autoComplete="current-password" required disabled={loading} placeholder="请输入密码"/><button type="button" className="password-toggle" aria-label={visible ? '隐藏密码' : '显示密码'} aria-pressed={visible} onClick={() => setVisible(!visible)} disabled={loading}>{visible ? '隐藏' : '显示'}</button></div></div>
      <button type="submit" className="btn btn-primary login-submit" disabled={loading}>{loading ? <><span className="spinner"/>正在登录…</> : <>登录工作台<Icon name="arrow" size={17}/></>}</button><p className="login-help">账号由管理员管理。如无法登录，请核对账号状态与当前密码。</p>
    </form>
  </div><p className="login-footer">知识有来源 · 内容可复核 · 过程可追踪</p></div>
}

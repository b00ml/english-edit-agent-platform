// App.tsx —— 应用外壳：登录守卫 + 左侧导航 + 右侧内容区
import { useEffect, useState } from 'react'
import { Link, Navigate, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import GeneratePage from './pages/GeneratePage'
import TasksPage from './pages/TasksPage'
import QualityPage from './pages/QualityPage'
import ContentLibraryPage from './pages/ContentLibraryPage'
import CostsPage from './pages/CostsPage'
import NotificationsPage from './pages/NotificationsPage'
import TracePage from './pages/TracePage'
import DashboardPage from './pages/DashboardPage'
import SamplesPage from './pages/SamplesPage'
import KnowledgeBasePage from './pages/KnowledgeBasePage'
import UserManagePage from './pages/UserManagePage'
import ModelSettingsPage from './pages/ModelSettingsPage'
import LoginPage from './pages/LoginPage'
import { getUnreadCount } from './api/client'
import {
  clearAuth,
  getCurrentUser,
  hasPermission,
  isLoggedIn,
} from './api/auth'
import Icon from './components/Icon'

/** 导航项配置（含权限点，无权限则不显示） */
const NAV_ITEMS = [
  {to:'/', label:'内容生成', icon:'create', group:'内容生产', end:true, permission:'generate:create'},
  {to:'/tasks', label:'生成任务', icon:'tasks', group:'内容生产', end:false, permission:'task:read'},
  {to:'/quality', label:'人工质检', icon:'quality', group:'内容生产', end:false, permission:'quality:review'},
  {to:'/library', label:'内容库', icon:'library', group:'内容生产', end:false, permission:'content:read'},
  {to:'/knowledge', label:'知识库', icon:'book', group:'资料与样本', end:false, permission:'ops:read'},
  {to:'/samples', label:'样本库', icon:'samples', group:'资料与样本', end:false, permission:'ops:read'},
  {to:'/dashboard', label:'运营看板', icon:'dashboard', group:'运营观察', end:false, permission:'ops:read'},
  {to:'/costs', label:'成本分析', icon:'costs', group:'运营观察', end:false, permission:'ops:read'},
  {to:'/traces', label:'调用链路', icon:'trace', group:'运营观察', end:false, permission:'ops:read'},
  {to:'/notifications', label:'站内消息', icon:'bell', group:'运营观察', end:false, permission:null},
  {to:'/model-settings', label:'模型设置', icon:'settings', group:'系统配置', end:false, permission:'model:manage'},
  {to:'/users', label:'用户管理', icon:'users', group:'系统配置', end:false, permission:'user:manage'},
]

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  researcher: '教研员',
  reviewer: '质检员',
  viewer: '查看者',
}

export default function App() {
  const [unread, setUnread] = useState(0)
  const [, authChanged] = useState(0)
  const user = getCurrentUser()
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()

  const loggedIn = isLoggedIn() && Boolean(user)

  useEffect(() => {
    const sync = () => authChanged(version => version + 1)
    const crossTab = (event: StorageEvent) => {if (!event.key || event.key.startsWith('english_edit_')) sync()}
    window.addEventListener('english-edit-auth', sync);window.addEventListener('storage', crossTab)
    return () => {window.removeEventListener('english-edit-auth', sync);window.removeEventListener('storage', crossTab)}
  }, [])

  // 未登录时重定向到登录页
  useEffect(() => {
    if (!loggedIn && location.pathname !== '/login') {
      navigate('/login', { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn, location.pathname])

  // 轮询未读通知数（登录后每 30s）
  useEffect(() => {
    if (!loggedIn) return
    let alive = true
    const poll = () => {
      getUnreadCount()
        .then((r) => alive && setUnread(r.count))
        .catch(() => undefined)
    }
    poll()
    const timer = setInterval(poll, 30000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [loggedIn])

  useEffect(() => {setNavOpen(false)}, [location.pathname])
  useEffect(() => {
    if (!navOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const frame = requestAnimationFrame(() => document.querySelector<HTMLElement>('#workspace-nav a')?.focus())
    const close = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {setNavOpen(false); document.getElementById('nav-trigger')?.focus()}
      if (e.key === 'Tab') {
        const elements = Array.from(document.querySelectorAll<HTMLElement>('#workspace-nav a, #workspace-nav button')).filter(el => el.getClientRects().length > 0)
        const first = elements[0], last = elements[elements.length - 1]
        if (first && e.shiftKey && document.activeElement === first) {e.preventDefault();last.focus()}
        else if (last && !e.shiftKey && document.activeElement === last) {e.preventDefault();first.focus()}
      }
    }
    const desktop = matchMedia('(min-width: 981px)')
    const resize = () => {if (desktop.matches) setNavOpen(false)}
    desktop.addEventListener('change', resize)
    document.addEventListener('keydown', close)
    return () => {cancelAnimationFrame(frame);document.body.style.overflow = previousOverflow;desktop.removeEventListener('change', resize);document.removeEventListener('keydown', close)}
  }, [navOpen])

  // 登录页：独立布局，不渲染侧边栏
  if (!loggedIn) {
    return (
      <Routes>
        <Route path="*" element={<LoginPage />} />
      </Routes>
    )
  }

  const visibleNav = NAV_ITEMS.filter(
    (item) => item.permission === null || hasPermission(item.permission),
  )

  const handleLogout = () => {
    clearAuth()
    setUnread(0)
    navigate('/login', { replace: true })
  }

  const groups = Array.from(new Set(visibleNav.map(item => item.group)))
  const active = NAV_ITEMS.find(item => item.to === location.pathname)
  const homePath = visibleNav[0]?.to ?? '/notifications'
  const denied = active?.permission && !hasPermission(active.permission)
  if (location.pathname === '/login' || (location.pathname === '/' && denied)) return <Navigate to={homePath} replace/>
  return (
    <div className={`layout ${navOpen ? 'nav-open' : ''}`}>
      <a className="skip-link" href="#workspace-main">跳转到主内容</a>
      {navOpen && <button className="nav-backdrop" aria-label="收起导航" onClick={() => setNavOpen(false)}/>}
      <aside className="sidebar" id="workspace-nav" aria-label="工作台导航" role={navOpen ? 'dialog' : undefined} aria-modal={navOpen ? true : undefined}>
        <div className="sidebar-brand"><span className="brand-logo">E</span><div><span className="brand-name">英语内容工作台</span><span className="brand-caption">ENGLISH CONTENT STUDIO</span></div></div>
        <nav className="sidebar-nav" aria-label="主导航">{groups.map(group => <div className="nav-group" key={group}>
          <p className="nav-group-label">{group}</p>
          {visibleNav.filter(item => item.group === group).map(item => <NavLink key={item.to} to={item.to} end={item.end} onClick={() => setNavOpen(false)} className={({isActive}) => isActive ? 'nav-item active' : 'nav-item'}>
            <Icon name={item.icon}/><span>{item.label}</span>{item.to === '/notifications' && unread > 0 && <span className="nav-badge" aria-label={`${unread}条未读消息`}>{unread > 99 ? '99+' : unread}</span>}
          </NavLink>)}
        </div>)}</nav>
        <div className="sidebar-footer"><div className="sidebar-user"><span className="user-avatar">{(user?.display_name || user?.username || '用').slice(0,1)}</span><div><span className="sidebar-user-name">{user?.display_name || user?.username}</span><span className="user-role">{user ? ROLE_LABELS[user.role] ?? user.role : ''}</span></div><button className="icon-button" aria-label="退出登录" title="退出登录" onClick={handleLogout}><Icon name="logout"/></button></div></div>
      </aside>
      <div className="workspace-shell">
        <header className="workspace-topbar"><div className="topbar-path"><button className="icon-button nav-trigger" id="nav-trigger" aria-label="展开导航" aria-expanded={navOpen} aria-controls="workspace-nav" onClick={() => setNavOpen(!navOpen)}><Icon name="menu"/></button><span className="topbar-section">工作台</span><Icon name="chevron" size={14}/><strong>{active?.label ?? '内容工作台'}</strong></div><span className="workspace-context">个人教研 · 人工复核后发布</span></header>
        <main className="content" id="workspace-main" tabIndex={-1}>
          {denied ? <div className="page"><div className="card access-message"><Icon name="quality" size={32}/><h2>当前角色无此页面权限</h2><p>请使用左侧可访问的工作区；服务端仍独立检查所有操作权限。</p><Link className="btn btn-primary" to={homePath}>返回工作区</Link></div></div> :
        <Routes>
          <Route path="/" element={<GeneratePage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/quality" element={<QualityPage />} />
          <Route path="/library" element={<ContentLibraryPage />} />
          <Route path="/samples" element={<SamplesPage />} />
          <Route path="/knowledge" element={<KnowledgeBasePage />} />
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/costs" element={<CostsPage />} />
          <Route path="/traces" element={<TracePage />} />
          <Route path="/notifications" element={<NotificationsPage />} />
          <Route path="/model-settings" element={<ModelSettingsPage />} />
          <Route path="/users" element={<UserManagePage />} />
          <Route path="*" element={<Navigate to={homePath} replace/>} />
        </Routes>}
        </main>
      </div>
    </div>
  )
}

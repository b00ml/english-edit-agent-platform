// auth.ts —— 登录态管理（token 存储 / 当前用户 / 角色判断）
// token 与当前用户信息存 localStorage，供登录页与路由守卫使用。
import type { UserInfo } from './types'

const TOKEN_KEY = 'english_edit_token'
const USER_KEY = 'english_edit_user'

/** 读取 token，无则返回空串 */
export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

/** 保存登录态 */
export function saveAuth(token: string, user: UserInfo): void {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
  window.dispatchEvent(new Event('english-edit-auth'))
}

/** 清除登录态（登出） */
export function clearAuth(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  window.dispatchEvent(new Event('english-edit-auth'))
}

/** 当前是否已登录 */
export function isLoggedIn(): boolean {
  return Boolean(getToken())
}

/** 读取当前用户信息；未登录或数据损坏返回 null */
export function getCurrentUser(): UserInfo | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as UserInfo
  } catch {
    return null
  }
}

/** 判断当前用户是否拥有某权限点（与后端 ROLE_PERMISSIONS 对齐，供前端 UX 控制） */
const PERMISSIONS: Record<string, string[]> = {
  'user:manage': ['admin'],
  'model:manage': ['admin'],
  'template:manage': ['admin'],
  'generate:create': ['admin', 'researcher'],
  'task:read': ['admin', 'researcher'],
  'generate:cancel': ['admin', 'researcher'],
  'content:read': ['admin', 'researcher', 'reviewer', 'viewer'],
  'quality:review': ['admin', 'researcher', 'reviewer'],
  'content:publish': ['admin', 'researcher'],
  'ops:read': ['admin', 'researcher'],
  'ops:write': ['admin', 'researcher'],
}

/** 判断当前用户是否拥有指定权限点 */
export function hasPermission(permission: string): boolean {
  const user = getCurrentUser()
  if (!user || user.status !== 'active') return false
  const allowed = PERMISSIONS[permission] || []
  return allowed.includes(user.role)
}

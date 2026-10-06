// api/client.ts —— axios 实例与各接口封装
// 开发环境经 Vite 代理转发到 http://localhost:8000，生产环境由 nginx 转发
import axios from 'axios'
import { clearAuth, getToken, saveAuth } from './auth'
import type {
  CalibrationRecord,
  ContentItem,
  ContentListResult,
  CostDeepResult,
  CostRow,
  DashboardResult,
  GenerateRequest,
  GenerateResult,
  GenerationTask,
  KnowledgeListResult,
  KnowledgeCatalogResult,
  RagScopeMode,
  KnowledgePreviewResult,
  KnowledgeRetrieveResult,
  KnowledgeUploadResult,
  LoginResult,
  Notification,
  NotificationListResult,
  QualityRecord,
  QualityReviewRequest,
  SamplePoolItem,
  SamplePoolListResult,
  SampleSyncResult,
  TaskListResult,
  Template,
  TraceListResult,
  TraceStep,
  UnreadCount,
  UserInfo,
  UserListResult,
} from './types'

const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截：自动附加 Bearer token（K4 认证）
client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：401 未认证/凭证失效时清除本地登录态（由路由守卫跳转登录页）
client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      clearAuth()
    }
    return Promise.reject(error)
  },
)

/** 后端 FastAPI 错误响应的载荷结构（HTTPException 返回 {detail: "..."}） */
interface ApiErrorPayload {
  detail?: string | {loc?: (string | number)[]; msg?: string}[]
}

/**
 * 从任意异常中提取可展示的错误信息。
 *
 * - axios 错误优先取后端返回的 detail（如「题型模板不存在」）；
 * - 其次是 Error.message；
 * - 均无法识别时返回 fallback 兜底文案。
 */
export function getApiErrorMessage(err: unknown, fallback = '请求失败'): string {
  if (axios.isAxiosError<ApiErrorPayload>(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string' && detail) return detail
    if (Array.isArray(detail) && detail.length) return detail.map(item => `${item.loc?.filter(part => part !== 'body').join('.') || '参数'}：${item.msg || '格式不正确'}`).join('；')
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}

/** 发起一次生成任务 */
export function generate(data: GenerateRequest): Promise<GenerateResult> {
  return client.post('/generate', data).then((r) => r.data)
}

/** 查询单个任务详情与进度 */
export function getTask(taskId: string): Promise<GenerationTask> {
  return client.get(`/tasks/${taskId}`).then((r) => r.data)
}

/** 任务列表（分页） */
export function listTasks(page = 1, pageSize = 20): Promise<TaskListResult> {
  return client
    .get('/tasks', { params: { page, page_size: pageSize } })
    .then((r) => r.data)
}

/** 内容检索（可按 template_id / status 筛选） */
export function listContents(
  params: { template_id?: string; status?: string; page?: number; page_size?: number } = {},
): Promise<ContentListResult> {
  return client.get('/contents', { params }).then((r) => r.data)
}

/** 内容详情 */
export function getContent(contentId: string): Promise<ContentItem> {
  return client.get(`/contents/${contentId}`).then((r) => r.data)
}

/** 发布内容 */
export function publishContent(contentId: string): Promise<ContentItem> {
  return client.post(`/contents/${contentId}/publish`).then((r) => r.data)
}

/** 人工质检：通过/驳回 */
export function reviewContent(
  contentId: string,
  data: QualityReviewRequest,
): Promise<QualityRecord> {
  return client.post(`/quality/${contentId}/review`, data).then((r) => r.data)
}

/** 成本聚合（可按 题型/模型/任务 维度，可选按题型过滤） */
export function getCosts(
  groupBy: 'template' | 'model' | 'task' = 'template',
  templateId?: string,
): Promise<CostRow[]> {
  return client
    .get('/costs', {
      params: { group_by: groupBy, ...(templateId ? { template_id: templateId } : {}) },
    })
    .then((r) => r.data)
}

/** 深度成本报表（生成/质检阶段拆分 + token 细分 + 多维聚合 + 单条下钻） */
export function getCostDeep(
  params: { template_id?: string; task_id?: string; limit?: number } = {},
): Promise<CostDeepResult> {
  return client.get('/costs/deep', { params }).then((r) => r.data)
}

/** 指标看板聚合（PRD 15.1 口径） */
export function getDashboard(): Promise<DashboardResult> {
  return client.get('/dashboard').then((r) => r.data)
}

/** 题型模板列表 */
export function listTemplates(): Promise<Template[]> {
  return client.get('/templates').then((r) => r.data)
}

/** 站内通知列表（可只看未读） */
export function listNotifications(
  params: { unread_only?: boolean; page?: number; page_size?: number } = {},
): Promise<NotificationListResult> {
  return client.get('/notifications', { params }).then((r) => r.data)
}

/** 未读通知数（供角标轮询） */
export function getUnreadCount(): Promise<UnreadCount> {
  return client.get('/notifications/unread-count').then((r) => r.data)
}

/** 标记单条通知已读 */
export function markNotificationRead(id: string): Promise<Notification> {
  return client.post(`/notifications/${id}/read`).then((r) => r.data)
}

/** 标记全部通知已读 */
export function markAllNotificationsRead(): Promise<UnreadCount> {
  return client.post('/notifications/read-all').then((r) => r.data)
}

/** trace 链路列表（最近优先，按 trace_id 去重聚合） */
export function listTraces(limit = 50, offset = 0): Promise<TraceListResult> {
  return client
    .get('/traces', { params: { limit, offset } })
    .then((r) => r.data)
}

/** 按 trace_id 查询完整链路（所有 LLM 调用，按时间升序） */
export function getTrace(traceId: string): Promise<TraceStep[]> {
  return client.get(`/traces/${traceId}`).then((r) => r.data)
}

// ---------------------------------------------------------------------------
// 高质量回流样本（数据回流 / J2）
// ---------------------------------------------------------------------------

/** 将一条高质量内容沉淀为回流样本 */
export function createSample(data: {
  content_id: string
  source?: string
  purpose?: string
}): Promise<SamplePoolItem> {
  return client.post('/samples', data).then((r) => r.data)
}

/** 自动将全部高质量内容沉淀为样本（幂等） */
export function syncSamples(): Promise<SampleSyncResult> {
  return client.post('/samples/sync').then((r) => r.data)
}

/** 回流样本检索（可按 题型/知识点/用途/来源 过滤，分页） */
export function listSamples(
  params: {
    template_id?: string
    knowledge_point?: string
    purpose?: string
    source?: string
    page?: number
    page_size?: number
  } = {},
): Promise<SamplePoolListResult> {
  return client.get('/samples', { params }).then((r) => r.data)
}

/** 从回流样本库移除一条样本 */
export function deleteSample(sampleId: string): Promise<{ deleted: string }> {
  return client.delete(`/samples/${sampleId}`).then((r) => r.data)
}

/** 导出回流样本为 JSONL 语料文件（few-shot/微调用） */
export function exportSamples(
  params: {
    template_id?: string
    knowledge_point?: string
    purpose?: string
    source?: string
  } = {},
): Promise<void> {
  return client
    .get('/samples/export', { params, responseType: 'blob' })
    .then((r) => {
      // 将 blob 保存为 samples.jsonl 下载
      const url = window.URL.createObjectURL(r.data)
      const a = document.createElement('a')
      a.href = url
      a.download = 'samples.jsonl'
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.URL.revokeObjectURL(url)
    })
}

// ---------------------------------------------------------------------------
// RAG 知识库
// ---------------------------------------------------------------------------

/** 上传文本资料并分块索引 */
export function uploadKnowledgeText(data: {
  source_type: string
  source_name: string
  text: string
  knowledge_point?: string
}): Promise<KnowledgeUploadResult> {
  return client.post('/knowledge', data).then((r) => r.data)
}

/** 上传教研文档（txt/md/docx/pdf）并解析、分块索引 */
export function uploadKnowledgeFile(file: File, sourceType: string, knowledgePoint?: string, knowledgePoints: string[] = [], chunkLayout?: 'legacy' | 'structure'): Promise<KnowledgeUploadResult> {
  const form = new FormData()
  if (chunkLayout) form.append('chunk_layout', chunkLayout)
  form.append('file', file)
  form.append('source_type', sourceType)
  if (knowledgePoint) form.append('knowledge_point', knowledgePoint)
  form.append('knowledge_points', JSON.stringify(knowledgePoints))
  return client
    .post('/knowledge/upload', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((r) => r.data)
}

export function getKnowledgeCatalog(): Promise<KnowledgeCatalogResult> {
  return client.get('/knowledge/points').then((r) => r.data)
}

/** Local parser/chunker preview; never embeds or writes knowledge. */
export function previewKnowledgeFile(file: File, chunkLayout?: 'legacy' | 'structure'): Promise<KnowledgePreviewResult> {
  const form = new FormData()
  if (chunkLayout) form.append('chunk_layout', chunkLayout)
  form.append('file', file)
  return client.post('/knowledge/preview', form).then((r) => r.data)
}

/** 知识分块列表（分页，可按资料类型过滤） */
export function listKnowledge(
  params: { source_type?: string; page?: number; page_size?: number } = {},
): Promise<KnowledgeListResult> {
  return client.get('/knowledge', { params }).then((r) => r.data)
}

/** 删除一条知识分块 */
export function deleteKnowledge(chunkId: string): Promise<{ deleted: string }> {
  return client.delete(`/knowledge/${chunkId}`).then((r) => r.data)
}

/** 检索知识片段（供生成上下文参考） */
export function retrieveKnowledge(
  query: string,
  knowledge_point?: string,
  top_k?: number,
  options: { scope_mode?: RagScopeMode; source_name?: string; context_mode?: 'legacy' | 'relation' } = {},
): Promise<KnowledgeRetrieveResult> {
  return client
    .get('/knowledge/retrieve', { params: { query, knowledge_point, top_k, ...options } })
    .then((r) => r.data)
}

// ---------------------------------------------------------------------------
// 认证与用户管理（K4 权限）
// ---------------------------------------------------------------------------

/** 用户登录：校验账号密码，成功后保存登录态 */
export async function login(username: string, password: string): Promise<UserInfo> {
  const { data } = await client.post<LoginResult>('/auth/login', { username, password })
  saveAuth(data.access_token, data.user)
  return data.user
}

/** 获取当前登录用户信息 */
export function getMe(): Promise<UserInfo> {
  return client.get('/auth/me').then((r) => r.data)
}

/** 用户列表（仅管理员） */
export function listUsers(page = 1, pageSize = 20): Promise<UserListResult> {
  return client
    .get('/users', { params: { page, page_size: pageSize } })
    .then((r) => r.data)
}

/** 创建用户（仅管理员） */
export function createUser(data: {
  username: string
  password: string
  display_name?: string
  role?: string
}): Promise<UserInfo> {
  return client.post('/users', data).then((r) => r.data)
}

/** 更新用户（仅管理员）：改显示名/角色/密码/状态 */
export function updateUser(
  userId: string,
  data: {
    display_name?: string
    role?: string
    password?: string
    status?: string
  },
): Promise<UserInfo> {
  return client.patch(`/users/${userId}`, data).then((r) => r.data)
}

// ---------------------------------------------------------------------------
// 质检校准（J1 质量闭环）
// ---------------------------------------------------------------------------

/** 触发质检权重校准：用人工驳回样本反向校准 judge 维度权重 */
export function calibrateQuality(data: {
  template_id: string
  alpha?: number
  min_samples?: number
  min_fp?: number
}): Promise<CalibrationRecord> {
  return client.post('/quality/calibrate', data).then((r) => r.data)
}

/** 查询校准记录列表（可按模板过滤） */
export function getCalibrations(templateId?: string): Promise<CalibrationRecord[]> {
  return client
    .get('/quality/calibration', { params: templateId ? { template_id: templateId } : {} })
    .then((r) => r.data)
}

export interface KnowledgeDocumentSummary {
  chunk_layout?: string; index_input_signature?: string | null
  id: string; source_name: string; source_type: string; status: string; leaf_count: number
  parent_count: number; index_revision: number; ocr_job_id: string | null; parser_version: string
  embedding_models: string[]; source_hash: string; created_at: string
}
export const listKnowledgeDocuments = (page = 1) => client.get<{total: number; items: KnowledgeDocumentSummary[]}>('/knowledge/documents', {params: {page, page_size: 20}}).then(r => r.data)
export const removeKnowledgeDocument = (id: string) => client.delete<{deleted_document: string; deleted_chunks: number; ocr_source_retained: boolean}>(`/knowledge/documents/${id}`).then(r => r.data)


export const getKnowledgeStructure = (id: string) => client.get<import('./types').StructurePlan>(`/knowledge/documents/${id}/structure`).then(r => r.data)
export const reviewKnowledgeStructure = (id: string, body: {
  review_signature: string; expected_index_revision: number; source_reviewed: boolean
  accepted_edge_ids: string[]; rejected_edge_ids: string[]
}) => client.post<import('./types').StructurePlan>(`/knowledge/documents/${id}/structure-review`, body).then(r => r.data)


export interface ModelProviderConfig {id: string; name: string; base_url: string; status: 'enabled' | 'disabled'; has_api_key: boolean; config_hash: string}
export interface EditableModelProfile {id?: string; name: string; provider: string; provider_id: string | null; model_name: string; cost_tier: string; is_default: boolean; status: 'enabled' | 'disabled'; max_fallbacks: number; budget_per_task: number | null; tenant_id?: string | null}
export interface ModelTemplateRoute {template_id: string; name?: string; generation_profile: string | null; judge_profile: string | null; template_generation?: unknown; template_judge?: string | null}
export const getModelCapabilities = () => client.get<{credential_storage_ready: boolean; legacy_base_url: string; legacy_model: string; protocol: string}>('/model-settings/capabilities').then(r => r.data)
export const getModelProviders = () => client.get<ModelProviderConfig[]>('/model-settings/providers').then(r => r.data)
export const saveModelProvider = (data: {id?: string; name: string; base_url: string; api_key?: string; status: 'enabled' | 'disabled'}) => client.post<ModelProviderConfig>('/model-settings/providers', data).then(r => r.data)
export const deleteModelProvider = (id: string) => client.delete(`/model-settings/providers/${id}`)
export const probeModelProvider = (id: string) => client.post<{reachable: boolean; models: string[]; generation_verified: boolean}>(`/model-settings/providers/${id}/probe`).then(r => r.data)
export const getModelProfiles = () => client.get<EditableModelProfile[]>('/model-profiles').then(r => r.data)
export const saveModelProfile = (data: EditableModelProfile) => client.post<EditableModelProfile>('/model-profiles', data).then(r => r.data)
export const getModelRoutes = () => client.get<ModelTemplateRoute[]>('/model-settings/routes').then(r => r.data)
export const saveModelRoute = (data: ModelTemplateRoute) => client.post('/model-settings/routes', {template_id: data.template_id, generation_profile: data.generation_profile, judge_profile: data.judge_profile})


export function getTraceSnapshot(rowId: string): Promise<{row_id: string; replay_level: string; content_hash: string; data: Record<string, unknown>}> {
  return client.get(`/traces/records/${rowId}/snapshot`).then(r => r.data)
}
export function setSamplePurpose(sampleId: string, purpose: 'sft' | 'fewshot') {
  return client.patch(`/samples/${sampleId}/purpose`, null, {params: {purpose}}).then(r => r.data)
}


/** Cooperative cancellation: acknowledgement is not a terminal cancelled task. */
export function cancelGenerationTask(taskId: string): Promise<{task_id: string; status: string; message: string}> {
  return client.post(`/tasks/${taskId}/cancel`).then(r => r.data)
}

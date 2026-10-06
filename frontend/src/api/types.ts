// api/types.ts —— 与后端 FastAPI 响应模型对应的 TS 类型
// 字段与 backend/app/schemas.py 保持一致

/** 深度成本报表（P3/K2，对应 CostDeepOut） */
export interface CostStage {
  stage: string
  count: number
  total_cost: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  prompt_cost: number
  completion_cost: number
}

export interface CostByKey {
  name: string
  count: number
  total_cost: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface CostTrace {
  trace_id: string
  model?: string | null
  stage: string
  template_id?: string | null
  task_id?: string | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  total_tokens?: number | null
  cost?: number | null
  latency_ms?: number | null
  created_at: string
}

export interface CostDeepResult {
  total_cost: number
  total_count: number
  total_prompt_tokens: number
  total_completion_tokens: number
  total_tokens: number
  stages: CostStage[]
  by_template: CostByKey[]
  by_model: CostByKey[]
  by_task: CostByKey[]
  traces: CostTrace[]
}

// ---------------------------------------------------------------------------
// 指标看板
// ---------------------------------------------------------------------------
/** 看板核心指标项（对应 DashboardKpi） */
export interface DashboardKpi {
  key: string
  label: string
  value: number | null
  unit: string
  target?: number | null
  /** higher_better / lower_better */
  goal: string
}

/** 按题型聚合明细 */
export interface DashboardByTemplate {
  template_id: string
  generated: number
  pass_rate: number | null
}

/** 单条任务生产周期明细 */
export interface DashboardTaskLatency {
  task_id: string
  template_id: string
  status: string
  created_at?: string | null
  latency_s?: number | null
}

/** 指标看板聚合结果（对应 DashboardOut） */
export interface DashboardResult {
  kpis: DashboardKpi[]
  by_template: DashboardByTemplate[]
  task_latency: DashboardTaskLatency[]
  total_cost: number
  generated_count: number
  published_count: number
}

/** 发起生成请求的入参 */
export interface GenerateRequest {
  template_id: string
  params: Record<string, unknown>
  quantity: number
  tenant_id?: string | null
}

/** 发起生成后的响应 */
export interface GenerateResult {
  task_id: string
  status: string
}

export type TaskStatus = 'pending' | 'dispatched' | 'running' | 'awaiting_review' | 'succeeded' | 'partially_succeeded' | 'failed' | 'cancelled'

/** 生成任务（对应 TaskOut） */
export interface GenerationTask {
  id: string
  template_id: string
  quantity: number
  status: TaskStatus
  progress: number
  trace_ref?: string | null
  tenant_id?: string | null
  params: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** 任务列表（分页）响应 */
export interface TaskListResult {
  total: number
  items: GenerationTask[]
}

/** 内容条目（对应 ContentOut） */
export interface RagProvenance {
  status?: string
  require_review?: boolean
  citations?: {chunk_id: string; segment_id?: string; page_no?: number | null; block_id?: string; source_name: string; content_hash: string; content?: string}[]
  reference_review?: {verified?: boolean; reviewer?: string}
}

export interface ContentItem {
  id: string
  task_id: string
  template_id: string
  payload: Record<string, unknown>
  provenance?: RagProvenance | null
  validation_report?: {
    valid: boolean
    errors: {code: string; path: string; message: string}[]
    warnings: {code: string; path: string; message: string}[]
    semantic_verification: string
  } | null
  qc_score?: number | null
  status: string
  revise_count: number
  cost?: number | null
  created_at: string
  updated_at: string
}

/** 内容列表（分页）响应 */
export interface ContentListResult {
  total: number
  items: ContentItem[]
}

/** 人工质检标注入参 */
export interface QualityReviewRequest {
  reference_verified?: boolean
  pass: boolean
  reason: string
}

/** 质检规则维度（对应模板 quality_rules 元素） */
export interface QualityRule {
  id: string
  weight: number
}

/** 质检记录（对应 QualityOut） */
export interface QualityRecord {
  id: string
  item_id: string
  score: number
  dimension_scores: Record<string, unknown>
  source: string
  reviewer?: string | null
  reason?: string | null
  created_at: string
}

/** 题型模板（对应 TemplateOut） */
export interface Template {
  id: string
  type_id: string
  name: string
  version: number
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  quality_rules: QualityRule[]
  gen_prompt: Record<string, unknown>
  run_config: Record<string, unknown>
  status: string
  created_at: string
  updated_at: string
}

/** 成本聚合行（对应 CostOut，按题型/模型/任务维度归并） */
export interface CostRow {
  name: string
  total_cost: number
  count: number
}

/** 站内消息通知（对应 NotificationOut） */
export interface Notification {
  id: string
  type: string
  title: string
  content: string
  related_id?: string | null
  is_read: boolean
  created_at: string
}

/** 通知列表（分页）+ 未读数 */
export interface NotificationListResult {
  total: number
  unread: number
  items: Notification[]
}

/** 未读通知数 */
export interface UnreadCount {
  count: number
}

// ---------------------------------------------------------------------------
// Trace 链路回放
// ---------------------------------------------------------------------------
/** 单条 TraceLog 记录（链路中的一步 LLM 调用，对应 TraceOut） */
export interface TraceStep {
  id: string
  snapshot_status?: string
  trace_id: string
  task_id?: string | null
  template_id?: string | null
  item_id?: string | null
  prompt_version?: string | null
  model?: string | null
  input_data?: Record<string, unknown> | null
  output_data?: Record<string, unknown> | null
  latency_ms?: number | null
  cost?: number | null
  /** 链路阶段：generate 生成 / qc 质检 */
  stage: string
  created_at: string
}

/** trace 链路摘要（列表项，对应 TraceSummaryOut） */
export interface TraceSummary {
  trace_id: string
  task_id?: string | null
  template_id?: string | null
  call_count: number
  total_cost: number
  total_latency_ms: number
  first_at: string
  last_at: string
}

/** trace 列表（分页，对应 TraceListOut） */
export interface TraceListResult {
  total: number
  items: TraceSummary[]
}

// ---------------------------------------------------------------------------
// 高质量回流样本（数据回流 / J2）
// ---------------------------------------------------------------------------
/** 回流样本条目（对应 SamplePoolOut） */
export interface SamplePoolItem {
  id: string
  item_id: string
  template_id: string
  source: string
  purpose: string
  knowledge_point?: string | null
  payload: Record<string, unknown>
  meta?: Record<string, unknown> | null
  created_at: string
}

/** 回流样本列表（分页，对应 SamplePoolListOut） */
export interface SamplePoolListResult {
  total: number
  items: SamplePoolItem[]
}

/** 自动同步结果（对应 SampleSyncOut） */
export interface SampleSyncResult {
  added: number
  total: number
}

// ---------------------------------------------------------------------------
// RAG 知识库
// ---------------------------------------------------------------------------
/** 知识分块条目（对应 KnowledgeChunkOut） */
export type RagScopeMode = 'exact' | 'ancestor' | 'descendant' | 'related' | 'semantic'
export interface KnowledgePoint {
  id: string
  canonical_name: string
  aliases: string[]
  parent_id?: string | null
}
export interface KnowledgeCatalogResult {
  catalog_hash: string
  items: KnowledgePoint[]
  defaults: { top_k: number; scope_mode: RagScopeMode; method: string; chunk_layout: 'legacy' | 'structure'; context_mode: 'legacy' | 'relation'; context_max_chars: number }
}

export interface KnowledgeChunk {
  id: string
  source_type: string
  source_name: string
  knowledge_point?: string | null
  content: string
  created_at: string
  parent_chunk_id?: string | null
  chunk_type?: string | null
  knowledge_point_labels?: string[] | null
  document_id?: string | null
  page_no?: number | null
  context_header?: string | null
  section_path?: string[] | null
  parser_version?: string | null
  chunker_version?: string | null
  meta?: Record<string, unknown> | null
}

/** 知识分块列表（分页，对应 KnowledgeListOut） */
export interface KnowledgeListResult {
  total: number
  items: KnowledgeChunk[]
}

/** 文件/文本上传索引结果（对应 KnowledgeUploadOut） */
export interface ChunkDiagnostics {
  layout?: string; source_coverage_scope?: string; excluded_navigation_blocks?: string[]; plan_signature?: string | null
  strategy_used: string
  chunk_count: number
  coverage_ratio: number
  hard_splits: number
  warnings: string[]
}

export interface KnowledgePreviewResult {
  chunk_layout: 'legacy' | 'structure';
  structure?: StructurePlan | null;
  indexable: boolean
  reason_code?: string | null
  message?: string | null
  document: { warnings: string[]; source_name: string; parser_version: string }
  chunks: { content: string; embedding_content: string; content_start: number | null; content_end: number | null; pages?: number[]; source_segments?: Record<string, unknown>[] }[]
  diagnostics: ChunkDiagnostics
  parents?: { content: string; child_indexes: number[] }[]
}

export interface KnowledgeUploadResult {
  chunks: number
  warnings?: string[]
  diagnostics?: ChunkDiagnostics
  source_type: string
  source_name: string
  knowledge_point?: string | null
}

/** 检索结果（对应 KnowledgeRetrieveOut） */
export interface KnowledgeRetrieveResult {
  protocol_version?: string;
  bundles?: ContextBundle[];
  query: string
  snippets: string[]
  citations: Record<string, unknown>[]
  diagnostics: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// 认证与用户管理（K4 权限）
// ---------------------------------------------------------------------------
/** 当前登录用户信息（对应 UserOut） */
export interface UserInfo {
  id: string
  username: string
  display_name: string
  role: string
  status: string
  created_at: string
}

/** 登录响应（对应 LoginOut） */
export interface LoginResult {
  access_token: string
  token_type: string
  user: UserInfo
}

/** 用户列表（分页，对应 UserListOut） */
export interface UserListResult {
  total: number
  items: UserInfo[]
}

/** 合法角色 */
export const ROLES = ['admin', 'researcher', 'reviewer', 'viewer'] as const

// ---------------------------------------------------------------------------
// 质检校准（J1 质量闭环）
// ---------------------------------------------------------------------------
/** 校准记录（对应 CalibrationOut） */
export interface CalibrationRecord {
  id: string
  template_id: string
  weights: Record<string, number>
  threshold: number
  default_weights: Record<string, number>
  default_threshold: number
  sample_size: number
  false_pass_cnt: number
  lenient: Record<string, number>
  rejection_rate: number
  note: string
  created_at: string
}

export interface StructureEdge {
  id: string; from: string; to: string; relation: string
  state: 'accepted' | 'proposed' | 'rejected'; evidence: string[]
}
export interface StructurePlan {
  version: string; signature: string; policy_hash: string
  source_scope_signature?: string; review_signature?: string
  index_revision?: number; structure_revision?: number; expansion_allowed?: boolean
  sections: { id: string; parent_id: string | null; level: number; title: string }[]
  units: { id: string; section_path: string[]; member_ids: string[] }[]
  blocks: {block_id: string; page_no: number | null; role: string; section_path: string[]; content_start: number; content_end: number}[]
  edges: StructureEdge[]; barriers: {block_id: string; reason: string}[]
  decisions: {accepted_edge_ids: string[]; rejected_edge_ids: string[]}
  logical_tables?: {id: string; physical_tables: {block_id:string; table_id:string; page_no:number; header_confirmed:boolean}[]; cell_edges: StructureEdge[]}[]
  unresolved_references?: {from:string; target:string; reason:string}[]
  counts: {sections: number; units: number; source_segments: number; accepted_edges: number; proposed_edges: number; furniture: number}
}
export interface SourceSegment {
  segment_id: string; chunk_id: string; document_id: string | null; page_no: number | null
  block_id: string | null; content: string; content_hash: string; source_content_hash: string
  content_start: number | null; content_end: number | null; section_path: string[]
  source_role: string; context_role: string; truncated: boolean; partial_row: boolean
  table_id: string | null; source_locator: Record<string, unknown>[]
}
export interface ContextBundle {
  id: string; seed_chunk_id: string; document_id: string | null; pages: number[]
  section_path: string[]; complete: boolean; incomplete_reasons: string[]
  source_segments: SourceSegment[]; rendered_context: string
  logical_table_views?: {logical_table_id:string; rendered_table:string; confirmed_cell_joins:string[]; physical_table_ids:string[]; rows:Record<string,unknown>[]; derived_view:boolean}[]
}

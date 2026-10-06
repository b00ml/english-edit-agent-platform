import StructurePreview from './StructurePreview'
import type { ContextBundle } from '../api/types'
// KnowledgeBasePage.tsx —— 知识库：上传教研文档（试卷/练习册等）建立 RAG 知识库，浏览/检索/删除
import KnowledgeDocuments from './KnowledgeDocuments'
import OCRWorkbench from './OCRWorkbench'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  deleteKnowledge,
  getApiErrorMessage,
  getKnowledgeCatalog,
  listKnowledge,
  previewKnowledgeFile,
  retrieveKnowledge,
  uploadKnowledgeFile,
} from '../api/client'
import type { KnowledgeChunk, KnowledgePoint, KnowledgePreviewResult, RagScopeMode } from '../api/types'

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024

const SOURCE_TYPES = ['教材', '课标', '真题', '练习册', '其他']

const SOURCE_TYPE_OPTIONS = SOURCE_TYPES.map((s) => ({ value: s, label: s }))

export default function KnowledgeBasePage() {
  const [documentRefresh, setDocumentRefresh] = useState(0)
  const [chunks, setChunks] = useState<KnowledgeChunk[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  // 筛选
  const [filterType, setFilterType] = useState('')
  // 上传
  const [sourceType, setSourceType] = useState('真题')
  const [ingestionLayout,setIngestionLayout] = useState<'legacy'|'structure'>('structure')
  const [file, setFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [preview, setPreview] = useState<KnowledgePreviewResult | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const [knowledgePoint, setKnowledgePoint] = useState('')
  const [multiPoints, setMultiPoints] = useState('')
  const [knownPoints, setKnownPoints] = useState<KnowledgePoint[]>([])
  const [searchPoint, setSearchPoint] = useState('')
  const [contextMode,setContextMode] = useState<'relation'|'legacy'>('relation')
  const [bundles,setBundles] = useState<ContextBundle[]>([])
  const [scopeMode, setScopeMode] = useState<RagScopeMode>('exact')
  const [topK, setTopK] = useState<number | undefined>()
  const [searchSource, setSearchSource] = useState('')
  const [retrievalInfo, setRetrievalInfo] = useState<Record<string, unknown> | null>(null)
  const [citations, setCitations] = useState<Record<string, unknown>[]>([])
  const fileInputRef = useRef<HTMLInputElement>(null)
  // 检索
  const [query, setQuery] = useState('')
  const [snippets, setSnippets] = useState<string[]>([])
  const [searching, setSearching] = useState(false)

  const load = useCallback(
    async (p: number) => {
      setLoading(true)
      setError('')
      try {
        const res = await listKnowledge({
          source_type: filterType || undefined,
          page: p,
          page_size: 20,
        })
        setChunks(res.items)
        setTotal(res.total)
      } catch (e) {
        setError(getApiErrorMessage(e, '知识库加载失败'))
      } finally {
        setLoading(false)
      }
    },
    [filterType],
  )

  useEffect(() => {
    load(page)
  }, [load, page])

  useEffect(() => {
    let mounted = true
    getKnowledgeCatalog().then((catalog) => {
      if (!mounted) return
      setKnownPoints(catalog.items)
      setScopeMode(catalog.defaults.scope_mode)
      setTopK(catalog.defaults.top_k)
      setIngestionLayout(catalog.defaults.chunk_layout)
      setContextMode(catalog.defaults.context_mode)
    }).catch((e) => { if (mounted) setError(getApiErrorMessage(e, '知识点目录加载失败')) })
    return () => { mounted = false }
  }, [])

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null
    setFile(f)
    setPreview(null)
    setWarnings([])
    setError(f && f.size > MAX_UPLOAD_BYTES ? '文件超过 10MB 上限，请先按页拆分；扫描 PDF 还需要 OCR。' : '')
    setSuccess('')
  }

  const handleUpload = async () => {
    if (!file) {
      setError('请先选择要上传的文件')
      return
    }
    if (file.size > MAX_UPLOAD_BYTES || (preview && !preview.indexable)) {
      setError(file.size > MAX_UPLOAD_BYTES ? '文件超过 10MB 上限，请先按页拆分。' : (preview?.message ?? '文件没有可索引文字，需先 OCR。'))
      return
    }
    setUploading(true)
    setError('')
    setSuccess('')
    try {
      const res = await uploadKnowledgeFile(file, sourceType, knowledgePoint.trim() || undefined, multiPoints.split(/[,，]/).map((v) => v.trim()).filter(Boolean), ingestionLayout)
      setSuccess(`已上传「${file.name}」并索引 ${res.chunks} 个分块`)
      setWarnings([...(res.warnings ?? []), ...(res.diagnostics?.warnings ?? [])])
      setFile(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      setPage(1)
      await load(1)
    } catch (e) {
      setError(getApiErrorMessage(e, '文件上传失败'))
    } finally {
      setUploading(false)
    }
  }

  const handlePreview = async () => {
    if (!file || file.size > MAX_UPLOAD_BYTES) return
    setPreviewing(true)
    setError('')
    try {
      const result = await previewKnowledgeFile(file,ingestionLayout)
      setPreview(result)
      setWarnings([...result.document.warnings, ...result.diagnostics.warnings])
    } catch (e) {
      setError(getApiErrorMessage(e, '解析预览失败'))
    } finally {
      setPreviewing(false)
    }
  }

  const handleDelete = async (id: string) => {
    if (!window.confirm('确认删除该知识分块？')) return
    try {
      await deleteKnowledge(id)
      setSuccess('已删除')
      await load(page)
      setDocumentRefresh(v => v + 1)
    } catch (e) {
      setError(getApiErrorMessage(e, '删除失败'))
    }
  }

  const handleSearch = async () => {
    if (!query.trim()) return
    setSearching(true)
    setError('')
    try {
      const res = await retrieveKnowledge(query.trim(), searchPoint.trim() || undefined, topK, { scope_mode: scopeMode, source_name: searchSource.trim() || undefined, context_mode: contextMode })
      setSnippets(res.snippets)
      setRetrievalInfo(res.diagnostics)
      setBundles(res.bundles ?? [])
      setCitations(res.citations)
    } catch (e) {
      setError(getApiErrorMessage(e, '检索失败'))
    } finally {
      setSearching(false)
    }
  }

  const totalPages = Math.max(1, Math.ceil(total / 20))

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">RAG 知识库</h2>
      </div>

      {error && <div className="alert-error">{error}</div>}
      {success && <div className="alert-success">{success}</div>}
      {warnings.length > 0 && (
        <div className="card" role="status">
          <h3 className="section-title">解析与切块提示（请核对原文）</h3>
          <ul>{[...new Set(warnings)].map((warning) => <li key={warning}>{warning}</li>)}</ul>
        </div>
      )}

      <OCRWorkbench onIndexed={() => { void load(page); setDocumentRefresh(v => v + 1) }} />
      <KnowledgeDocuments refresh={documentRefresh} onChanged={() => { void load(page); setDocumentRefresh(v => v + 1) }} />

      {/* 文件上传 */}
      <div className="card">
        <h3 className="section-title">上传教研文档</h3>
        <div className="filter-bar">
          <div className="form-group">
            <label className="form-label">资料类型</label>
            <select
              className="form-control"
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
            >
              {SOURCE_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <div className="form-group form-group-grow">
            <label className="form-label">文件（txt / md / docx / pdf / html / xlsx / csv）</label>
            <input
              ref={fileInputRef}
              className="form-control"
              type="file"
              disabled={uploading || previewing}
              accept=".txt,.md,.markdown,.docx,.pdf,.html,.htm,.xlsx,.csv"
              onChange={handleFileChange}
            />
          </div>
          <div className="form-group">
            <label className="form-label">知识点标签（与生成参数一致）</label>
            <input className="form-control" value={knowledgePoint} list="knowledge-point-options"
              onChange={(e) => setKnowledgePoint(e.target.value)} maxLength={128} />
          </div>
          <div className="form-group">
            <label className="form-label">附加知识点（逗号分隔，最多 16 个）</label>
            <input className="form-control" value={multiPoints} onChange={(e) => setMultiPoints(e.target.value)} />
          </div>
          <label>文件切分布局<select aria-label="文件切分布局" className="form-control" value={ingestionLayout} onChange={e=>{setIngestionLayout(e.target.value as 'legacy'|'structure');setPreview(null)}}><option value="legacy">旧版兼容</option><option value="structure">结构单元 / 逐段来源</option></select></label>
          <datalist id="knowledge-point-options">
            {knownPoints.map((point) => <option key={point.id} value={point.canonical_name}>{point.aliases.join(' / ')}</option>)}
          </datalist>
          <button className="btn btn-secondary" onClick={handlePreview}
            disabled={!file || file.size > MAX_UPLOAD_BYTES || uploading || previewing}>
            {previewing ? '解析中…' : '预览解析与切块（不调用模型）'}
          </button>
          <button
            className="btn btn-primary"
            onClick={handleUpload}
            disabled={!file || file.size > MAX_UPLOAD_BYTES || uploading || previewing || (preview !== null && !preview.indexable)}
          >
            {uploading ? '上传中…' : '上传并索引'}
          </button>
        </div>
        <p className="hint">支持上传试卷、练习册、讲义等教研文档，单文件上限 10MB；扫描 PDF 须先 OCR。预览不会调用模型，正式索引会调用已配置的 embedding 服务。</p>
      </div>

      {preview && (
        <div className="card">
          <h3 className="section-title">解析预览</h3>
          {!preview.indexable && <div className="alert-error" role="alert">{preview.message ?? '文件没有可索引文字，请先进行 OCR。'}</div>}
          <p className="hint">
            策略：{preview.diagnostics.strategy_used} · 分块：{preview.chunks.length} ·
            {preview.diagnostics.layout === 'structure' ? '可索引正文覆盖率：' : '文字覆盖率：'}{preview.indexable ? `${(preview.diagnostics.coverage_ratio * 100).toFixed(1)}%` : '不适用（没有文字）'} ·
            硬切：{preview.diagnostics.hard_splits}（不是召回率或事实正确率）
          </p>
          {(preview.parents ?? []).map((parent, index) => (
            <details key={`parent-${index}`}><summary>父块 {index + 1} · 包含子块 {parent.child_indexes.map((i) => i + 1).join(', ')}</summary>
              <pre style={{ whiteSpace: 'pre-wrap' }}>{parent.content}</pre>
            </details>
          ))}
          {preview.structure && <StructurePreview plan={preview.structure}/>}
          {preview.chunks.map((chunk, index) => (
            <details key={index}>
              <summary>分块 {index + 1} · {chunk.source_segments?.length ? `多段来源：${chunk.source_segments.length}段 / 页${chunk.pages?.join(', ') ?? '未知'}` : `规范化原文坐标 ${chunk.content_start}–${chunk.content_end}`}</summary>
              <pre style={{ whiteSpace: 'pre-wrap' }}>{chunk.embedding_content}</pre>
            </details>
          ))}
        </div>
      )}

      {/* 检索 */}
      <div className="card">
        <h3 className="section-title">检索测试</h3>
        <div className="filter-bar">
          <div className="form-group form-group-grow">
            <input
              className="form-control"
              placeholder="输入检索问题，查看命中的知识片段…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
          </div>
          <input className="form-control" aria-label="检索知识点" placeholder="知识点 / 别名（可选）"
            list="knowledge-point-options" value={searchPoint} onChange={(e) => setSearchPoint(e.target.value)} />
          <select className="form-control" aria-label="知识点召回模式" value={scopeMode} onChange={(e) => setScopeMode(e.target.value as RagScopeMode)}>
            <option value="exact">精确知识点</option><option value="ancestor">包含祖先知识点</option>
            <option value="descendant">包含子孙知识点</option><option value="related">关联知识点</option>
            <option value="semantic">语义检索（不按知识点过滤）</option>
          </select>
          <input className="form-control" aria-label="资料名称过滤" placeholder="资料名称（精确匹配，可选）"
            value={searchSource} onChange={(e) => setSearchSource(e.target.value)} />
          <select className="form-control" aria-label="检索返回数量" value={topK ?? ''}
            onChange={(e) => setTopK(e.target.value ? Number(e.target.value) : undefined)}>
            <option value="">服务端默认</option>
            {[...new Set([5, 8, ...(topK === undefined ? [] : [topK])])].sort((a, b) => a - b).map((value) => (
              <option key={value} value={value}>Top {value} · 最多 {value} 个上下文包</option>
            ))}
          </select>
          <select className="form-control" aria-label="上下文返回策略" value={contextMode} onChange={e=>setContextMode(e.target.value as 'relation'|'legacy')}><option value="relation">结构关联 / 小块检索大块返回</option><option value="legacy">旧版页内上下文</option></select>
          <button className="btn" onClick={handleSearch} disabled={searching}>
            {searching ? '检索中…' : '检 索'}
          </button>
        </div>
        {snippets.length > 0 && (
          <div className="snippet-list">
            {snippets.map((s, i) => (
              <div key={i} className="snippet-item">
                {s}
              </div>
            ))}
          </div>
        )}
      </div>

      {retrievalInfo && (
        <div className="card">
          <h3 className="section-title">召回诊断与来源（不代表事实已核验）</h3>
          <p>请求 Top {String(retrievalInfo.requested_top_k ?? '未知')} · 候选池 {String(retrievalInfo.candidate_pool ?? 0)} · 种子 {String(retrievalInfo.seed_count ?? 0)} · 上下文包 {String(retrievalInfo.bundle_count ?? 0)} · 实际来源段 {String(retrievalInfo.segment_count ?? 0)}。结构成员完整不等于问题已正确回答。</p>
          <p className="hint">Top 5 / Top 8 是返回包上限，不是保证数量；总上下文预算保持 {String(retrievalInfo.context_max_chars ?? 0)} 字符，关联扩展、去重和预算限制可能使实际返回更少。</p>
          {bundles.map(bundle=><details key={bundle.id}><summary>来源包 · 页 {bundle.pages.join(', ') || '未定位'} · {bundle.complete?'确认关系成员已返回':'存在未返回/不确定成员'} · {bundle.source_segments.length}段</summary>
            <p>{bundle.section_path.join(' > ')} · {bundle.incomplete_reasons.join(', ')}</p>
            {bundle.logical_table_views?.map(view=><details key={view.logical_table_id}><summary>逻辑表 · {view.physical_table_ids.join(', ')} · 确认单元格续接 {view.confirmed_cell_joins.length}</summary><p>派生结构视图，不覆盖以下原始来源；跨页/列映射在诊断中保留。</p><pre className="ocr-text">{view.rendered_table}</pre></details>)}
            {bundle.source_segments.map(segment=><article className="ocr-block" key={segment.segment_id}><p>{segment.chunk_id} · 页{segment.page_no ?? '未知'} · {segment.block_id} · {segment.source_role} · 原坐标{segment.content_start}–{segment.content_end} · {segment.truncated||segment.partial_row?'部分片段':'完整片段'}</p><pre className="ocr-text">{segment.content}</pre><p className="hint">hash {segment.content_hash}</p></article>)}
          </details>)}
          <details><summary>查询扩展、各路召回、RRF / rerank 与降级</summary>
            <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(retrievalInfo, null, 2)}</pre>
          </details>
          <details><summary>来源引用 / 父子关系 / 截断状态</summary>
            <pre style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(citations, null, 2)}</pre>
          </details>
        </div>
      )}

      {/* 分块列表 */}
      <div className="card table-card">
        <div className="table-toolbar">
          <div className="form-group">
            <label className="form-label">资料类型筛选</label>
            <select
              className="form-control"
              value={filterType}
              onChange={(e) => {
                setFilterType(e.target.value)
                setPage(1)
              }}
            >
              <option value="">全部</option>
              {SOURCE_TYPE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
          <span className="table-count">共 {total} 个分块</span>
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>来源</th>
              <th>类型</th>
              <th>知识点</th>
              <th>内容</th>
              <th>入库时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {chunks.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.source_name}</td>
                <td>{c.source_type}</td>
                <td>{c.knowledge_point ?? '-'}</td>
                <td className="cell-clamp">{c.content}</td>
                <td>{new Date(c.created_at).toLocaleString()}</td>
                <td>
                  <button className="btn-link-danger" onClick={() => handleDelete(c.id)}>
                    删除
                  </button>
                </td>
              </tr>
            ))}
            {chunks.length === 0 && (
              <tr>
                <td colSpan={6} className="empty">
                  {loading ? '加载中…' : '暂无知识分块，请先上传文档'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {totalPages > 1 && (
          <div className="pagination">
            <button
              className="btn"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              上一页
            </button>
            <span>
              {page} / {totalPages}
            </span>
            <button
              className="btn"
              disabled={page >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            >
              下一页
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

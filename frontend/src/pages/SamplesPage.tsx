// SamplesPage.tsx —— 样本库：高质量回流样本（few-shot/微调语料）浏览、沉淀、导出
import { useCallback, useEffect, useState } from 'react'
import {
  deleteSample,
  exportSamples,
  getApiErrorMessage,
  setSamplePurpose,
  listSamples,
  listTemplates,
  syncSamples,
} from '../api/client'
import ContentPreview from '../components/ContentPreview'
import type { ContentItem, SamplePoolItem, Template } from '../api/types'

/** 沉淀来源中文标签 */
const SOURCE_LABEL: Record<string, string> = {
  manual: '人工沉淀',
  auto: '自动同步',
}

/** 语料用途中文标签 */
const PURPOSE_LABEL: Record<string, string> = {
  sft: '微调语料',
  fewshot: 'Few-shot',
}

export default function SamplesPage() {
  const [templates, setTemplates] = useState<Template[]>([])
  const [items, setItems] = useState<SamplePoolItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  // 筛选条件
  const [filterTpl, setFilterTpl] = useState('')
  const [filterSource, setFilterSource] = useState('')
  // 预览
  const [preview, setPreview] = useState<SamplePoolItem | null>(null)

  useEffect(() => {
    listTemplates()
      .then(setTemplates)
      .catch(() => undefined)
  }, [])

  const load = useCallback(
    async (p: number) => {
      setLoading(true)
      setError('')
      try {
        const res = await listSamples({
          template_id: filterTpl || undefined,
          source: filterSource || undefined,
          page: p,
          page_size: 20,
        })
        setItems(res.items)
        setTotal(res.total)
      } catch (e) {
        setError(getApiErrorMessage(e, '样本库加载失败'))
      } finally {
        setLoading(false)
      }
    },
    [filterTpl, filterSource],
  )

  useEffect(() => {
    load(page)
  }, [page, load])

  // 筛选条件变化时回到第一页
  useEffect(() => {
    setPage(1)
  }, [filterTpl, filterSource])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  /** 自动同步全部高质量内容为样本 */
  const handleSync = async () => {
    setError('')
    setSuccess('')
    try {
      const res = await syncSamples()
      setSuccess(`已自动同步，新增 ${res.added} 条，当前共 ${res.total} 条样本`)
      load(page)
    } catch (e) {
      setError(getApiErrorMessage(e, '同步失败'))
    }
  }

  /** 导出 JSONL 语料 */
  const handleExport = async () => {
    setError('')
    setSuccess('')
    try {
      await exportSamples({
        template_id: filterTpl || undefined,
        source: filterSource || undefined,
      })
      setSuccess('已导出样本语料（samples.jsonl）')
    } catch (e) {
      setError(getApiErrorMessage(e, '导出失败'))
    }
  }

  /** 移除样本 */
  const handleDelete = async (sample: SamplePoolItem) => {
    setError('')
    try {
      await deleteSample(sample.id)
      load(page)
    } catch (e) {
      setError(getApiErrorMessage(e, '移除失败'))
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">样本库</h2>
        <div className="header-actions">
          <button className="btn btn-ghost" onClick={handleSync} disabled={loading}>
            自动沉淀
          </button>
          <button className="btn btn-primary" onClick={handleExport} disabled={loading}>
            导出语料
          </button>
          <button className="btn btn-ghost" onClick={() => load(page)} disabled={loading}>
            刷新
          </button>
        </div>
      </div>

      {success && <div className="alert-success">{success}</div>}

      {/* 筛选栏 */}
      <div className="card filter-bar">
        <div className="form-group">
          <label className="form-label">题型</label>
          <select
            className="form-control"
            value={filterTpl}
            onChange={(e) => setFilterTpl(e.target.value)}
          >
            <option value="">全部</option>
            {templates.map((t) => (
              <option key={t.type_id} value={t.type_id}>
                {t.name}（{t.type_id}）
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">来源</label>
          <select
            className="form-control"
            value={filterSource}
            onChange={(e) => setFilterSource(e.target.value)}
          >
            <option value="">全部</option>
            <option value="manual">人工沉淀</option>
            <option value="auto">自动同步</option>
          </select>
        </div>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="card table-card">
        <table className="table">
          <thead>
            <tr>
              <th>样本 ID</th>
              <th>题型</th>
              <th>知识点</th>
              <th>用途</th>
              <th>来源</th>
              <th>质检分</th>
              <th>沉淀时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td className="mono">{s.id}</td>
                <td>{s.template_id}</td>
                <td>{s.knowledge_point ?? '-'}</td>
                <td>{PURPOSE_LABEL[s.purpose] ?? s.purpose}</td>
                <td>
                  <span className={`status-tag status-${s.source === 'auto' ? 'published' : 'passed'}`}>
                    {SOURCE_LABEL[s.source] ?? s.source}
                  </span>
                </td>
                <td>{s.meta?.qc_score != null ? String(s.meta.qc_score) : '-'}</td>
                <td className="mono">{new Date(s.created_at).toLocaleString()}</td>
                <td>
                  <button className="btn btn-ghost" onClick={() => setPreview(s)}>
                    预览
                  </button>
                  <button className="btn btn-ghost" onClick={async () => {
                    try {await setSamplePurpose(s.id, s.purpose === 'fewshot' ? 'sft' : 'fewshot'); setSuccess('用途已更新；few-shot仍需实际人工记录、当前规则与来源校验'); load(page)} catch(e) {setError(getApiErrorMessage(e, '用途更新失败'))}
                  }}>{s.purpose === 'fewshot' ? '改为微调语料' : '用于 Few-shot'}</button>
                  <button className="btn btn-ghost" onClick={() => handleDelete(s)}>
                    移除
                  </button>
                </td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8} className="empty">
                  {loading ? '加载中…' : '暂无回流样本。先「自动沉淀」或将人工通过的内容沉淀为样本。'}
                </td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="pagination">
          <span className="pagination-total">共 {total} 条</span>
          <button
            className="btn btn-ghost"
            disabled={page <= 1}
            onClick={() => setPage((p) => p - 1)}
          >
            上一页
          </button>
          <span>
            {page} / {totalPages}
          </span>
          <button
            className="btn btn-ghost"
            disabled={page >= totalPages}
            onClick={() => setPage((p) => p + 1)}
          >
            下一页
          </button>
        </div>
      </div>

      {/* 样本预览弹窗：将样本载荷包装为 ContentItem 复用通用预览 */}
      {preview && (
        <div className="modal-mask" onClick={() => setPreview(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>样本预览</h3>
              <button className="btn btn-ghost" onClick={() => setPreview(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <ContentPreview
                item={{ template_id: preview.template_id, payload: preview.payload } as ContentItem}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

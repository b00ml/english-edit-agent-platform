// QualityPage.tsx —— 质检页：按状态/题型查看内容并人工标注
import { useCallback, useEffect, useState } from 'react'
import {
  getApiErrorMessage,
  listContents,
  listTemplates,
  reviewContent,
} from '../api/client'
import ContentPreview from '../components/ContentPreview'
import type { ContentItem, Template } from '../api/types'

/** 内容状态中文标签 */
const STATUS_LABEL: Record<string, string> = {
  pending_qc: '待质检',
  awaiting_review: '等待人工裁决',
  passed: '已通过',
  rejected: '已驳回',
  published: '已发布',
}

/** 状态筛选选项（'' 表示全部） */
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: '全部状态' },
  ...Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label })),
]

export default function QualityPage() {
  const [items, setItems] = useState<ContentItem[]>([])
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 筛选条件
  const [status, setStatus] = useState('pending_qc')
  const [templateId, setTemplateId] = useState('')
  const [templates, setTemplates] = useState<Template[]>([])
  // 当前查看/质检的内容
  const [current, setCurrent] = useState<ContentItem | null>(null)
  // 质检表单
  const [reason, setReason] = useState('')
  const [acting, setActing] = useState(false)
  const [referenceVerified, setReferenceVerified] = useState(false)
  useEffect(() => setReferenceVerified(false), [current?.id])

  const load = useCallback(
    async (p: number) => {
      setLoading(true)
      setError('')
      try {
        const res = await listContents({
          status: status || undefined,
          template_id: templateId || undefined,
          page: p,
          page_size: pageSize,
        })
        setItems(res.items)
        setTotal(res.total)
      } catch (e) {
        setError(getApiErrorMessage(e, '质检列表加载失败'))
      } finally {
        setLoading(false)
      }
    },
    [pageSize, status, templateId],
  )

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  useEffect(() => {
    load(page)
  }, [page, load])

  // 加载题型列表用于筛选
  useEffect(() => {
    listTemplates()
      .then(setTemplates)
      .catch(() => setTemplates([]))
  }, [])

  // 筛选条件变化时回到第一页
  useEffect(() => {
    setPage(1)
  }, [status, templateId])

  /** 提交质检结论 */
  const handleReview = async (pass: boolean) => {
    if (!current) return
    if (!pass && !reason.trim()) {
      setError('驳回时需要填写原因')
      return
    }
    setActing(true)
    setError('')
    try {
      await reviewContent(current.id, { pass, reason: reason.trim(), reference_verified: referenceVerified })
      setCurrent(null)
      setReason('')
      // 刷新列表
      load(page)
    } catch (e) {
      setError(getApiErrorMessage(e, '质检提交失败'))
    } finally {
      setActing(false)
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">人工质检</h2>
        <button className="btn btn-primary" onClick={() => load(page)} disabled={loading}>
          刷新
        </button>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="card filter-bar">
        <div className="form-group">
          <label className="form-label">状态筛选</label>
          <select
            className="form-control"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            {STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group">
          <label className="form-label">题型筛选</label>
          <select
            className="form-control"
            value={templateId}
            onChange={(e) => setTemplateId(e.target.value)}
          >
            <option value="">全部题型</option>
            {templates.map((t) => (
              <option key={t.type_id} value={t.type_id}>
                {t.name}（{t.type_id}）
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="card table-card">
        <table className="table">
          <thead>
            <tr>
              <th>内容 ID</th>
              <th>题型</th>
              <th>质检分</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              return (
                <tr key={item.id}>
                  <td className="mono">{item.id}</td>
                  <td>{item.template_id}</td>
                  <td>{item.qc_score ?? '-'}</td>
                  <td>
                    <span className={`status-tag status-${item.status}`}>
                      {STATUS_LABEL[item.status] ?? item.status}
                    </span>
                  </td>
                  <td>
                    <button className="btn btn-ghost" onClick={() => setCurrent(item)}>
                      质检
                    </button>
                  </td>
                </tr>
              )
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">
                  {loading ? '加载中…' : '暂无待质检内容'}
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

      {/* 质检弹窗 */}
      {current && (
        <div className="modal-mask" onClick={() => setCurrent(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>内容质检</h3>
              <button className="btn btn-ghost" onClick={() => setCurrent(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <ContentPreview item={current} />
              {current.provenance && (
                <div className="form-group">
                  <p>知识来源状态：{current.provenance.status ?? '未记录'}（未自动证明答案正确）</p>
                  <ul>{current.provenance.citations?.map((c, index) => (
                    <li key={c.segment_id ?? `${c.chunk_id}:${index}`} data-source-segment={c.segment_id}>
                      {c.source_name} · 页 {c.page_no ?? '未知'} · {c.block_id ?? '旧引用'} · {c.chunk_id}
                      {c.content && <pre className="ocr-text">{c.content}</pre>}
                    </li>
                  ))}</ul>
                  {current.provenance.require_review && <label><input type="checkbox" checked={referenceVerified} onChange={e => setReferenceVerified(e.target.checked)} /> 我已核对上述来源与题目、答案及解析</label>}
                </div>
              )}
              <div className="form-group">
                <label className="form-label">驳回原因</label>
                <textarea
                  className="form-control"
                  rows={3}
                  placeholder="驳回时填写原因（可选）"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </div>
              <div className="form-actions">
                <button
                  className="btn btn-danger"
                  disabled={acting}
                  onClick={() => handleReview(false)}
                >
                  驳回
                </button>
                <button
                  className="btn btn-primary"
                  disabled={acting || current.validation_report?.valid === false || (Boolean(current.provenance?.require_review) && !referenceVerified)}
                  onClick={() => handleReview(true)}
                >
                  通过
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
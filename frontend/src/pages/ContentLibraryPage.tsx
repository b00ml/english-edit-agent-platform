// ContentLibraryPage.tsx —— 内容库：检索筛选、预览、发布
import { useCallback, useEffect, useState } from 'react'
import { getApiErrorMessage, listContents, listTemplates, publishContent } from '../api/client'
import ContentPreview from '../components/ContentPreview'
import type { ContentItem, Template } from '../api/types'

/** 内容状态中文标签 */
const STATUS_LABEL: Record<string, string> = {
  pending_qc: '待质检',
  passed: '已通过',
  rejected: '已驳回',
  published: '已发布',
}

export default function ContentLibraryPage() {
  const [templates, setTemplates] = useState<Template[]>([])
  const [items, setItems] = useState<ContentItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  // 筛选条件
  const [filterTpl, setFilterTpl] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  // 预览内容
  const [preview, setPreview] = useState<ContentItem | null>(null)

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
        const res = await listContents({
          template_id: filterTpl || undefined,
          status: filterStatus || undefined,
          page: p,
          page_size: 20,
        })
        setItems(res.items)
        setTotal(res.total)
      } catch (e) {
        setError(getApiErrorMessage(e, '内容库加载失败'))
      } finally {
        setLoading(false)
      }
    },
    [filterTpl, filterStatus],
  )

  useEffect(() => {
    load(page)
  }, [page, load])

  // 筛选条件变化时回到第一页
  useEffect(() => {
    setPage(1)
  }, [filterTpl, filterStatus])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  /** 发布内容 */
  const handlePublish = async (item: ContentItem) => {
    try {
      await publishContent(item.id)
      load(page)
    } catch (e) {
      setError(getApiErrorMessage(e, '发布失败'))
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">内容库</h2>
        <button className="btn btn-primary" onClick={() => load(page)} disabled={loading}>
          刷新
        </button>
      </div>

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
          <label className="form-label">状态</label>
          <select
            className="form-control"
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
          >
            <option value="">全部</option>
            <option value="pending_qc">待质检</option>
            <option value="passed">已通过</option>
            <option value="rejected">已驳回</option>
            <option value="published">已发布</option>
          </select>
        </div>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="card table-card">
        <table className="table">
          <thead>
            <tr>
              <th>内容 ID</th>
              <th>题型</th>
              <th>知识点</th>
              <th>难度</th>
              <th>质检分</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item) => {
              const payload = item.payload as {
                knowledge_point?: string
                difficulty?: string
              }
              return (
                <tr key={item.id}>
                  <td className="mono">{item.id}</td>
                  <td>{item.template_id}</td>
                  <td>{payload?.knowledge_point ?? '-'}</td>
                  <td>{payload?.difficulty ?? '-'}</td>
                  <td>{item.qc_score ?? '-'}</td>
                  <td>
                    <span className={`status-tag status-${item.status}`}>
                      {STATUS_LABEL[item.status] ?? item.status}
                    </span>
                  </td>
                  <td>
                    <button className="btn btn-ghost" onClick={() => setPreview(item)}>
                      预览
                    </button>
                    <button
                      className="btn btn-ghost"
                      disabled={item.status !== 'passed' || item.validation_report?.valid === false}
                      onClick={() => handlePublish(item)}
                    >
                      {item.status === 'published' ? '已发布' : '发布'}
                    </button>
                  </td>
                </tr>
              )
            })}
            {items.length === 0 && (
              <tr>
                <td colSpan={7} className="empty">
                  {loading ? '加载中…' : '暂无内容'}
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

      {/* 内容预览弹窗 */}
      {preview && (
        <div className="modal-mask" onClick={() => setPreview(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>内容预览</h3>
              <button className="btn btn-ghost" onClick={() => setPreview(null)}>
                关闭
              </button>
            </div>
            <div className="modal-body">
              <ContentPreview item={preview} />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
// TracePage.tsx —— 链路回放：按 trace_id 查看完整生成链路（生成→质检各步 LLM 调用）
import { useEffect, useState } from 'react'
import { getApiErrorMessage, getTrace, getTraceSnapshot, listTraces } from '../api/client'
import type { TraceStep, TraceSummary } from '../api/types'

function formatCost(v: number | null | undefined): string {
  if (v == null) return '-'
  return `¥${v.toFixed(6)}`
}

function formatLatency(v: number | null | undefined): string {
  if (v == null) return '-'
  return `${v.toFixed(0)} ms`
}

function formatShortTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
}

export default function TracePage() {
  const [summaries, setSummaries] = useState<TraceSummary[]>([])
  const [selectedTraceId, setSelectedTraceId] = useState('')
  const [inputTraceId, setInputTraceId] = useState('')
  const [steps, setSteps] = useState<TraceStep[]>([])
  const [loadingList, setLoadingList] = useState(false)
  const [loadingSteps, setLoadingSteps] = useState(false)
  const [error, setError] = useState('')
  const [snapshot, setSnapshot] = useState<Record<string, unknown> | null>(null)
  const [snapshotBusy, setSnapshotBusy] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const loadList = () => {
    setLoadingList(true)
    listTraces()
      .then((r) => setSummaries(r.items))
      .catch((e) => setError(getApiErrorMessage(e, '链路列表加载失败')))
      .finally(() => setLoadingList(false))
  }

  useEffect(() => {
    loadList()
  }, [])

  const loadTrace = (traceId: string) => {
    if (!traceId) return
    setSelectedTraceId(traceId)
    setInputTraceId(traceId)
    setLoadingSteps(true)
    setError('')
    getTrace(traceId)
      .then((r) => {
        setSteps(r)
        setExpanded({})
      })
      .catch((e) => {
        setError(getApiErrorMessage(e, '链路详情加载失败'))
        setSteps([])
      })
      .finally(() => setLoadingSteps(false))
  }

  const handleSearch = () => {
    loadTrace(inputTraceId.trim())
  }

  const toggleExpand = (id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }))
  }

  const totalCost = steps.reduce((s, st) => s + (st.cost || 0), 0)
  const totalLatency = steps.reduce((s, st) => s + (st.latency_ms || 0), 0)

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">链路回放</h2>
        <button className="btn btn-ghost" onClick={loadList} disabled={loadingList}>
          刷 新
        </button>
      </div>

      {error && <div className="alert-error">{error}</div>}

      <div className="card filter-bar">
        <div className="form-group" style={{ flex: 1, minWidth: 320, marginBottom: 0 }}>
          <label className="form-label">trace_id</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <input
              className="form-control"
              placeholder="输入 trace_id（即 task_id）查询链路"
              value={inputTraceId}
              onChange={(e) => setInputTraceId(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
            <button className="btn btn-primary" onClick={handleSearch} disabled={loadingSteps}>
              查询
            </button>
          </div>
        </div>
      </div>

      <div className="trace-grid">
        {/* 左侧：trace 列表 */}
        <div className="card table-card">
          <table className="table">
            <thead>
              <tr>
                <th>trace_id</th>
                <th>调用数</th>
                <th>成本</th>
                <th>最近时间</th>
              </tr>
            </thead>
            <tbody>
              {summaries.map((s) => (
                <tr
                  key={s.trace_id}
                  className={s.trace_id === selectedTraceId ? 'selected' : ''}
                  onClick={() => loadTrace(s.trace_id)}
                  style={{ cursor: 'pointer' }}
                  title={s.trace_id}
                >
                  <td className="mono">{s.trace_id.slice(0, 12)}…</td>
                  <td>{s.call_count}</td>
                  <td>{formatCost(s.total_cost)}</td>
                  <td>{formatShortTime(s.last_at)}</td>
                </tr>
              ))}
              {summaries.length === 0 && (
                <tr>
                  <td colSpan={4} className="empty">
                    {loadingList ? '加载中…' : '暂无链路记录'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {/* 右侧：链路详情 */}
        <div>
          {steps.length === 0 ? (
            <div className="card trace-empty">
              {loadingSteps ? '加载中…' : '选择左侧链路或输入 trace_id 查看详情'}
            </div>
          ) : (
            <>
              <div className="card trace-summary-bar">
                <span className="mono trace-id-full">{selectedTraceId}</span>
                <span className="trace-summary-stat">调用 {steps.length} 次</span>
                <span className="trace-summary-stat">耗时 {formatLatency(totalLatency)}</span>
                <span className="trace-summary-stat">成本 {formatCost(totalCost)}</span>
              </div>
              <div className="trace-steps">
                {steps.map((step, idx) => (
                  <div key={step.id} className="trace-step">
                    <div className="trace-step-head" onClick={() => toggleExpand(step.id)}>
                      <span className="trace-step-idx">{idx + 1}</span>
                      <span
                        className={`status-tag ${step.stage === 'qc' ? 'status-running' : 'status-pending'}`}
                      >
                        {step.stage === 'qc' ? '质检' : '生成'}
                      </span>
                      <span className="mono">{step.model || '-'}</span>
                      <span className="trace-step-meta">{formatLatency(step.latency_ms)}</span>
                      <span className="trace-step-meta">{formatCost(step.cost)}</span>
                      <span className="trace-step-time">{formatShortTime(step.created_at)}</span>
                    </div>
                    {expanded[step.id] && (
                      <div className="trace-step-body">
                        <p>受限快照：{step.snapshot_status ?? 'legacy_unavailable'}（摘要仍脱敏/截断；快照读取会审计，旧记录不能补出完整Prompt）</p>
                        <button className="btn btn-ghost" disabled={snapshotBusy || step.snapshot_status !== 'available'} onClick={async () => {
                          setSnapshotBusy(true); setError('')
                          try {setSnapshot(await getTraceSnapshot(step.id))} catch(e) {setError(getApiErrorMessage(e, '快照不可用'))} finally {setSnapshotBusy(false)}
                        }}>查看受限请求／响应快照</button>

                        {step.input_data && (
                          <div className="trace-step-section">
                            <div className="form-label">输入</div>
                            <pre className="pre">
                              {JSON.stringify(step.input_data, null, 2)}
                            </pre>
                          </div>
                        )}
                        {step.output_data && (
                          <div className="trace-step-section">
                            <div className="form-label">输出</div>
                            <pre className="pre">
                              {JSON.stringify(step.output_data, null, 2)}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
      {snapshot && <div className="modal-mask" onClick={() => setSnapshot(null)}><div className="modal" onClick={e => e.stopPropagation()}><div className="modal-header"><h3>受限脱敏快照</h3><button className="btn btn-ghost" onClick={() => setSnapshot(null)}>关闭快照</button></div><div className="modal-body"><p>只能回放保存时的脱敏请求／响应，不保证模型重跑结果一致。超过预算、过期或旧调用不可补造。</p><pre className="pre">{JSON.stringify(snapshot,null,2)}</pre></div></div></div>}
    </div>
  )
}

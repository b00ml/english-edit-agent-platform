// DashboardPage.tsx —— 指标看板：生产量/质检通过率/人工驳回率/生产周期/成本
// 口径对齐 PRD 第 15.1 节
// 含质检校准中心（J1 质量闭环）：用驳回样本反向校准 judge 权重
import { useEffect, useState } from 'react'
import {
  calibrateQuality,
  getApiErrorMessage,
  getCalibrations,
  getDashboard,
  listTemplates,
} from '../api/client'
import type { CalibrationRecord, DashboardResult, DashboardKpi, Template } from '../api/types'

function formatValue(kpi: DashboardKpi): string {
  const { value, unit } = kpi
  if (value == null) return "暂无样本"
  if (unit === '%') return `${value}%`
  if (unit === '¥') return `¥${value.toFixed(4)}`
  if (unit === 's') {
    // 秒 -> 更可读的分/时
    if (value >= 3600) return `${(value / 3600).toFixed(2)} h`
    if (value >= 60) return `${(value / 60).toFixed(1)} min`
    return `${value.toFixed(1)} s`
  }
  return `${value} ${unit}`.trim()
}

function isMet(kpi: DashboardKpi): boolean | null {
  if (kpi.target == null || kpi.value == null) return null
  return kpi.goal === 'lower_better' ? kpi.value <= kpi.target : kpi.value >= kpi.target
}

function formatLatency(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v >= 3600) return `${(v / 3600).toFixed(2)} h`
  if (v >= 60) return `${(v / 60).toFixed(1)} min`
  return `${v.toFixed(1)} s`
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)

  // 校准中心状态
  const [templates, setTemplates] = useState<Template[]>([])
  const [calibTemplate, setCalibTemplate] = useState('')
  const [calibrations, setCalibrations] = useState<CalibrationRecord[]>([])
  const [calibLoading, setCalibLoading] = useState(false)
  const [calibError, setCalibError] = useState('')
  const [calibSuccess, setCalibSuccess] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    getDashboard()
      .then(setData)
      .catch((e) => setError(getApiErrorMessage(e, '指标数据加载失败')))
      .finally(() => setLoading(false))
  }, [refreshKey])

  // 加载模板列表与校准记录
  useEffect(() => {
    listTemplates().then(setTemplates).catch(() => {})
    getCalibrations()
      .then(setCalibrations)
      .catch(() => {})
  }, [])

  const handleCalibrate = () => {
    if (!calibTemplate) {
      setCalibError('请选择题型')
      return
    }
    setCalibLoading(true)
    setCalibError('')
    setCalibSuccess('')
    calibrateQuality({ template_id: calibTemplate })
      .then((calib) => {
        setCalibSuccess(`校准完成：${calib.note}`)
        // 刷新校准记录列表
        getCalibrations().then(setCalibrations).catch(() => {})
      })
      .catch((e) => setCalibError(getApiErrorMessage(e, '校准失败')))
      .finally(() => setCalibLoading(false))
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2 className="page-title">指标看板</h2>
        <button
          className="btn btn-primary"
          onClick={() => setRefreshKey((k) => k + 1)}
          disabled={loading}
        >
          刷 新
        </button>
      </div>

      {error && <div className="alert-error">{error}</div>}
      {loading && !data && <div className="card trace-empty">加载中…</div>}

      {data && (
        <>
          {/* 核心 KPI 卡片 */}
          <div className="kpi-grid">
            {data.kpis.map((kpi) => {
              const met = isMet(kpi)
              return (
                <div key={kpi.key} className="card kpi-card">
                  <div className="kpi-label">{kpi.label}</div>
                  <div className="kpi-value">{formatValue(kpi)}</div>
                  {kpi.target != null && (
                    <div className="kpi-meta">
                      目标 {formatValue({ ...kpi, value: kpi.target })}
                      <span className={`kpi-badge ${met === null ? '' : met ? 'ok' : 'warn'}`}>
                        {met === null ? '—' : met ? '达标' : '未达标'}
                      </span>
                    </div>
                  )}
                </div>
              )
            })}
          </div>

          {/* 汇总条 */}
          <div className="card trace-summary-bar">
            <span className="trace-summary-stat">生成总数 {data.generated_count} 条</span>
            <span className="trace-summary-stat">已发布 {data.published_count} 条</span>
            <span className="trace-summary-stat">累计成本 ¥{data.total_cost.toFixed(4)}</span>
          </div>

          <div className="dashboard-grid">
            {/* 按题型产出与通过率 */}
            <div className="card table-card">
              <h3 className="section-title">按题型产出</h3>
              <table className="table">
                <thead>
                  <tr>
                    <th>题型</th>
                    <th>产出量</th>
                    <th>通过率</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_template.map((t) => (
                    <tr key={t.template_id}>
                      <td className="mono">{t.template_id}</td>
                      <td>{t.generated}</td>
                      <td>{t.pass_rate == null ? "暂无终态样本" : `${t.pass_rate}%`}</td>
                    </tr>
                  ))}
                  {data.by_template.length === 0 && (
                    <tr>
                      <td colSpan={3} className="empty">
                        暂无产出
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {/* 近 10 条任务生产周期 */}
            <div className="card table-card">
              <h3 className="section-title">最近任务生产周期</h3>
              <table className="table">
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>题型</th>
                    <th>状态</th>
                    <th>耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {data.task_latency.map((t) => (
                    <tr key={t.task_id}>
                      <td className="mono">{t.task_id.slice(0, 8)}…</td>
                      <td className="mono">{t.template_id}</td>
                      <td>{t.status}</td>
                      <td>{formatLatency(t.latency_s)}</td>
                    </tr>
                  ))}
                  {data.task_latency.length === 0 && (
                    <tr>
                      <td colSpan={4} className="empty">
                        暂无任务
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* 质检校准中心（J1 质量闭环） */}
          <div className="card table-card">
            <h3 className="section-title">质检校准中心</h3>
            <p className="calib-desc">
              用人工驳回样本反向校准 judge 维度权重：假阳性（auto 高分但 manual 驳回）的维度被降权，
              下次质检更准、驳回率下降。
            </p>
            <div className="calib-controls">
              <select
                className="select"
                value={calibTemplate}
                onChange={(e) => setCalibTemplate(e.target.value)}
              >
                <option value="">选择题型…</option>
                {templates.map((t) => (
                  <option key={t.type_id} value={t.type_id}>
                    {t.type_id}
                  </option>
                ))}
              </select>
              <button
                className="btn btn-primary"
                onClick={handleCalibrate}
                disabled={calibLoading || !calibTemplate}
              >
                {calibLoading ? '校准中…' : '一键校准'}
              </button>
            </div>
            {calibError && <div className="alert-error">{calibError}</div>}
            {calibSuccess && <div className="alert-success">{calibSuccess}</div>}

            {calibrations.length > 0 && (
              <table className="table calib-table">
                <thead>
                  <tr>
                    <th>题型</th>
                    <th>样本数</th>
                    <th>假阳性</th>
                    <th>驳回率</th>
                    <th>生效权重</th>
                    <th>默认权重</th>
                    <th>说明</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {calibrations.map((c) => (
                    <tr key={c.id}>
                      <td className="mono">{c.template_id}</td>
                      <td>{c.sample_size}</td>
                      <td>{c.false_pass_cnt}</td>
                      <td>{(c.rejection_rate * 100).toFixed(1)}%</td>
                      <td className="mono">
                        {Object.entries(c.weights).map(([k, v]) => (
                          <span key={k} className="weight-chip">
                            {k}: {v.toFixed(2)}
                          </span>
                        ))}
                      </td>
                      <td className="mono">
                        {Object.entries(c.default_weights).map(([k, v]) => (
                          <span key={k} className="weight-chip muted">
                            {k}: {v.toFixed(2)}
                          </span>
                        ))}
                      </td>
                      <td className="calib-note">{c.note}</td>
                      <td className="mono">{new Date(c.created_at).toLocaleString('zh-CN')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {calibrations.length === 0 && (
              <div className="trace-empty">暂无校准记录，请先积累人工抽检数据后触发校准</div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

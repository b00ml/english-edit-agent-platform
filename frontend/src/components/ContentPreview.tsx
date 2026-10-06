// components/ContentPreview.tsx —— 通用内容预览
// 按 payload 结构自适应渲染：单选(single_choice)、完形(cloze)、阅读(reading)
import type { ContentItem } from '../api/types'

interface ChoicePayload {
  stem?: string
  options?: string[]
  answer?: string
  explanation?: string
}

interface ClozeBlank {
  index?: number
  answer?: string
  options?: string[]
  explanation?: string
}

interface ClozePayload {
  passage?: string
  blanks?: ClozeBlank[]
}

interface ReadingQuestion {
  stem?: string
  options?: string[]
  answer?: string
  explanation?: string
}

interface ReadingPayload {
  passage?: string
  questions?: ReadingQuestion[]
}

/** Display one assigned option label; do not mutate stored model output or answers. */
function optionBody(option: string, index: number): string {
  const label = String.fromCharCode(65 + index)
  return option.replace(new RegExp(`^\\s*${label}[.、):：]\\s*`), '')
}

/** 渲染一题选择题（可用于单选/阅读小题） */
function renderChoice(stem: string, options: string[], answer: string, explanation: string) {
  return (
    <div className="preview-block">
      <p className="preview-stem">{stem}</p>
      {options.map((opt, i) => (
        <p key={i} className="preview-option">
          {String.fromCharCode(65 + i)}. {optionBody(opt, i)}
        </p>
      ))}
      <p className="preview-answer">答案：{answer}</p>
      <p className="preview-explanation">解析：{explanation}</p>
    </div>
  )
}

function PayloadPreview({ item }: { item: ContentItem }) {
  const ts = item.template_id
  const payload = item.payload as Record<string, any>

  // 单选：stem + options + answer + explanation
  if (ts === 'single_choice') {
    const p = payload as ChoicePayload
    return renderChoice(p.stem ?? '(无题干)', p.options ?? [], p.answer ?? '-', p.explanation ?? '-')
  }

  // 完形：passage + blanks
  if (ts === 'cloze') {
    const p = payload as ClozePayload
    return (
      <div>
        <p className="preview-passage">{p.passage ?? '(无短文)'}</p>
        {(p.blanks ?? []).map((b, i) => (
          <div className="preview-block" key={i}>
            <p className="preview-stem">
              第 {b.index ?? i + 1} 空（答案：{b.answer ?? '-'}）
            </p>
            {(b.options ?? []).map((opt, j) => (
              <p key={j} className="preview-option">
                {String.fromCharCode(65 + j)}. {optionBody(opt, j)}
              </p>
            ))}
            <p className="preview-explanation">解析：{b.explanation ?? '-'}</p>
          </div>
        ))}
      </div>
    )
  }

  // 阅读：passage + questions
  if (ts === 'reading') {
    const p = payload as ReadingPayload
    return (
      <div>
        <p className="preview-passage">{p.passage ?? '(无短文)'}</p>
        {(p.questions ?? []).map((q, i) => (
          <div className="preview-block" key={i}>
            <p className="preview-stem">
              {i + 1}. {q.stem ?? '(无题干)'}
            </p>
            {(q.options ?? []).map((opt, j) => (
              <p key={j} className="preview-option">
                {String.fromCharCode(65 + j)}. {optionBody(opt, j)}
              </p>
            ))}
            <p className="preview-answer">答案：{q.answer ?? '-'}</p>
            <p className="preview-explanation">解析：{q.explanation ?? '-'}</p>
          </div>
        ))}
      </div>
    )
  }

  // 兜底：原样输出 JSON
  return <pre className="pre">{JSON.stringify(payload, null, 2)}</pre>
}
export default function ContentPreview({ item }: { item: ContentItem }) {
  const report = item.validation_report
  return <div>
    {report && <div className="preview-block" role="note">
      {report.errors.map((issue, i) => <p key={`error-${i}`} className="preview-explanation">结构校验未通过：{issue.message}</p>)}
      {report.warnings.map((issue, i) => <p key={`warning-${i}`} className="preview-explanation">质量提示：{issue.message}</p>)}
      <p className="preview-explanation">结构校验不代表答案正确或唯一；请人工核对题目及干扰项。</p>
    </div>}
    <PayloadPreview item={item} />
  </div>
}

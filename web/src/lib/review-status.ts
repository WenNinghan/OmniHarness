/** Present explicit host-recorded degraded review states.
 * Missing, COMPLETED, or unrecognised values never fabricate a passed or failed review.
 */
export type DegradedReviewCode = 'INVALID_OUTPUT' | 'INCOMPLETE' | 'UNKNOWN' | 'NOT_RUN_BUDGET'

export interface ReviewStatusWarning {
  code: DegradedReviewCode
  label: string
  message: string
}

const DESCRIPTIONS: Record<DegradedReviewCode, { label: string; message: string }> = {
  INVALID_OUTPUT: {
    label: '审查未完成 · 格式无效',
    message: '审查模型返回格式不符合结构化契约；原始回复已保留在调用记录中，本次按未审查交付。',
  },
  INCOMPLETE: {
    label: '审查未完成 · 输出不完整',
    message: '审查模型输出被截断或未完整结束；已收到的内容保存在调用记录中，本次按未审查交付。',
  },
  UNKNOWN: {
    label: '审查未完成 · 结果待核对',
    message: '审查请求因网络、超时或上游中断未能确认结果；原请求保持待核对状态且不会自动重发，本次按未审查交付。',
  },
  NOT_RUN_BUDGET: {
    label: '审查未完成 · 预算耗尽',
    message: '审查阶段前模型或活跃时间预算已耗尽，本次未发起独立审查请求。',
  },
}

export function reviewStatusWarning(value: unknown): ReviewStatusWarning | null {
  if (typeof value !== 'string' || !(value in DESCRIPTIONS)) return null
  const code = value as DegradedReviewCode
  return { code, ...DESCRIPTIONS[code] }
}

export function reviewStatusCopyText(answer: string, value: unknown): string {
  const warning = reviewStatusWarning(value)
  if (!warning) return answer
  return `【独立审查未完成（${warning.code}）】${warning.message}\n\n${answer}`
}

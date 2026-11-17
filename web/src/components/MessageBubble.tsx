import { useState } from 'react'
import { Check, ChevronRight, CircleAlert, Copy, LoaderCircle, Sparkles } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { commandResultAuditCopyText, commandResultAuditWarning } from '../lib/command-result-audit.ts'
import { reviewStatusCopyText, reviewStatusWarning } from '../lib/review-status.ts'
import type { Message } from '../lib/types.ts'
import { stageLabels } from '../lib/types.ts'
import { Button } from './ui.tsx'
import './MessageBubble.css'

export function MessageBubble({ message, streaming, notify, onModelSettings }: { message: Message; streaming: boolean; notify: (text: string, error?: boolean) => void; onModelSettings?: () => void }) {
  const [copied, setCopied] = useState<'answer' | 'reasoning' | null>(null)
  async function copy(kind: 'answer' | 'reasoning') {
    try {
      await navigator.clipboard.writeText(kind === 'answer'
        ? commandResultAuditCopyText(
            reviewStatusCopyText(message.content, message.output_metadata?.review_status),
            message.output_metadata?.command_result_audit,
          )
        : message.reasoning_content ?? '')
      setCopied(kind); window.setTimeout(() => setCopied(null), 1500)
    } catch { notify('复制失败，请手动选择文本复制。', true) }
  }
  if (message.role === 'user') return <article className="user-message"><div>{message.content}</div></article>
  const reasoning = message.reasoning_content ?? ''
  const output = message.output_metadata?.output_status
  const stage = message.output_metadata?.stage
  const commandAudit = commandResultAuditWarning(message.output_metadata?.command_result_audit)
  const reviewWarning = reviewStatusWarning(message.output_metadata?.review_status)
  const stageLabel = stage && stage in stageLabels ? stageLabels[stage as keyof typeof stageLabels] : null
  const incomplete = ['failed', 'interrupted', 'cancelled'].includes(message.status) ||
    ['truncated', 'reasoning_only', 'empty', 'filtered', 'interrupted'].includes(output ?? '')
  const detail = message.error_message || (output === 'truncated' ? '已达到输出上限，回答尚未完成。可在模型连接中调整输出上限后重新发送。'
    : output === 'reasoning_only' ? '模型只返回了思考过程，尚未给出正式回答。'
      : message.status === 'cancelled' ? '生成已停止，已保留收到的内容。'
        : message.status === 'interrupted' ? '生成中断，已保留收到的内容。'
          : '本次生成未完成，请检查模型设置后重新发送。')
  return <article className="assistant-message">
    <span className="assistant-avatar"><Sparkles size={16} /></span>
    <div className="assistant-body">
      <div className="assistant-heading"><strong>OmniHarness</strong>{streaming && <span className="generating"><span />{reasoning && !message.content ? '正在思考' : '正在生成'}</span>}</div>
      {reasoning && <details className="reasoning-disclosure">
        <summary><ChevronRight className="reasoning-chevron" size={15} />{streaming && !message.content ? <LoaderCircle size={15} className="spin" /> : <Sparkles size={15} />}<span>{streaming && !message.content ? '正在思考' : incomplete ? '已收到思考过程' : '思考过程'}</span><small>展开查看</small></summary>
        <div className="reasoning-body"><span className="reasoning-source">模型提供的思考 · 与正式回答分开显示</span><div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{reasoning}</ReactMarkdown></div><Button type="button" variant="ghost" size="small" className="copy-message" onClick={() => void copy('reasoning')}>{copied === 'reasoning' ? <Check size={13} /> : <Copy size={13} />}{copied === 'reasoning' ? '已复制' : '复制思考过程'}</Button></div>
      </details>}
      {commandAudit?.kind === 'conflict' && <div className="message-output-notice" role="alert"><CircleAlert size={16} /><div><strong>命令退出码存在冲突</strong><p>检测到回答中的命令退出码与实际工具记录不一致，请在运行详情核对命令和日志。</p>{commandAudit.conflicts.map((item, index) => <p key={`${item.artifactId}-${index}`}>工件 <code>{item.artifactId}</code>：回答写退出码 {item.claimedExitCode}，工具记录为 {item.actualExitCode}。</p>)}{commandAudit.truncated && <p>核对范围或展示数量有限，请查看完整工具记录。</p>}</div></div>}
      {commandAudit?.kind === 'incomplete' && <div className="message-output-notice" role="status"><CircleAlert size={16} /><div><strong>命令退出码核对未完成</strong><p>部分内容或证据超出本次核对范围，请查看工具记录。</p></div></div>}
      {reviewWarning && <div className="message-output-notice" role="status"><CircleAlert size={16} /><div><strong>{reviewWarning.label}</strong><p>{reviewWarning.message}</p></div></div>}
      {message.content && <div className="markdown answer-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>{streaming && <span className="typing-cursor" />}</div>}
      {streaming && !message.content && !reasoning && <div className="response-waiting"><LoaderCircle size={14} className="spin" />等待模型响应…</div>}
      {message.status === 'paused' && <div className="response-paused">已暂停，收到的内容已保存。</div>}
      {incomplete && <div className="message-output-notice" role="status"><CircleAlert size={16} /><div><strong>{stageLabel ? `${stageLabel}阶段未完成` : output === 'truncated' ? '回答未完成' : message.status === 'cancelled' ? '已停止生成' : '本次生成未完成'}</strong><p>{detail}</p>{onModelSettings && ['truncated', 'reasoning_only'].includes(output ?? '') && <Button type="button" variant="ghost" size="small" onClick={onModelSettings}>调整模型输出上限</Button>}</div></div>}
      {message.content && !streaming && <Button type="button" className="copy-message" variant="ghost" size="small" onClick={() => void copy('answer')}>{copied === 'answer' ? <Check size={13} /> : <Copy size={13} />}{copied === 'answer' ? '已复制' : '复制回答'}</Button>}
    </div>
  </article>
}

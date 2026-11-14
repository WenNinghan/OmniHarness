import type { Message, Run, RunEvent } from './types.ts'
import { terminalStatuses } from './types.ts'

type MessageState = Pick<Message, 'output_metadata' | 'error_code' | 'error_message'> & { status?: string }
export interface StreamState { events: RunEvent[]; text: Record<string, string>; reasoning: Record<string, string>; outputs: Record<string, MessageState>; lastSequence: number }
export const emptyStream = (): StreamState => ({ events: [], text: {}, reasoning: {}, outputs: {}, lastSequence: 0 })

// A terminal Run is immutable: a late request cannot bring it back to life.
export function mergeRunSnapshot(current: Run | null, incoming: Run): Run | null {
  if (!current || current.id !== incoming.id) return current
  if (terminalStatuses.has(current.status) && current.status !== incoming.status) return current
  return incoming
}

export function acceptsSnapshot(
  requestVersion: number, currentVersion: number,
  selectedConversation: string | null, expectedConversation: string,
  selectedRun: string | undefined, expectedRun: string,
): boolean {
  return requestVersion === currentVersion && selectedConversation === expectedConversation && selectedRun === expectedRun
}

function accumulate(state: StreamState, event: RunEvent): StreamState {
  const { message_id, delta, output_metadata } = event.payload
  let { text, reasoning, outputs } = state
  if (typeof message_id === 'string') {
    if (event.event_type === 'message.delta' && typeof delta === 'string') text = { ...text, [message_id]: (text[message_id] ?? '') + delta }
    if (event.event_type === 'message.reasoning_delta' && typeof delta === 'string') reasoning = { ...reasoning, [message_id]: (reasoning[message_id] ?? '') + delta }
    const statuses: Record<string, string> = { 'run.completed': 'completed', 'run.failed': 'failed', 'run.interrupted': 'interrupted', 'run.cancelled': 'cancelled', 'run.paused': 'paused' }
    if (event.event_type === 'message.output_state' || statuses[event.event_type]) {
      const next: MessageState = { ...outputs[message_id] }
      if (output_metadata && typeof output_metadata === 'object' && !Array.isArray(output_metadata)) next.output_metadata = { ...next.output_metadata, ...output_metadata }
      if (statuses[event.event_type]) next.status = statuses[event.event_type]
      const code = event.payload.error_code ?? event.payload.code
      const error = event.payload.error_message ?? event.payload.message
      if (typeof code === 'string') next.error_code = code
      if (typeof error === 'string') next.error_message = error
      outputs = { ...outputs, [message_id]: next }
    }
  }
  return { events: [...state.events, event], text, reasoning, outputs, lastSequence: event.sequence_number }
}

// Replay reconstructs each channel independently; a saved snapshot is never an append buffer.
export function applyEvent(state: StreamState, event: RunEvent): StreamState {
  if (state.events.some(item => item.event_id === event.event_id || (item.run_id === event.run_id && item.sequence_number === event.sequence_number))) return state
  if (event.sequence_number > state.lastSequence) return accumulate(state, event)
  return [...state.events, event].sort((a, b) => a.sequence_number - b.sequence_number).reduce(accumulate, emptyStream())
}

function coherentText(saved: string, replay: string | undefined): string {
  return replay !== undefined && replay.startsWith(saved) ? replay : saved
}

export function mergeMessages(history: Message[], stream: StreamState, conversationId: string, runId: string): Message[] {
  const messages = history.map(message => {
    const live = stream.outputs[message.id] ?? {}
    const status = ['completed', 'failed', 'cancelled', 'interrupted'].includes(message.status) ? message.status : live.status ?? message.status
    return { ...message, ...live, status, content: coherentText(message.content, stream.text[message.id]),
      reasoning_content: coherentText(message.reasoning_content ?? '', stream.reasoning[message.id]),
      output_metadata: { ...message.output_metadata, ...live.output_metadata } }
  })
  for (const id of new Set([...Object.keys(stream.text), ...Object.keys(stream.reasoning), ...Object.keys(stream.outputs)])) {
    if (!messages.some(message => message.id === id)) {
      const event = stream.events.find(item => item.payload.message_id === id)
      messages.push({ id, conversation_id: conversationId, run_id: runId, role: 'assistant', content: stream.text[id] ?? '',
        reasoning_content: stream.reasoning[id] ?? '', output_metadata: {}, status: 'streaming', created_at: event?.created_at ?? '', ...stream.outputs[id] })
    }
  }
  return messages
}

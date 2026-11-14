import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { cva } from 'class-variance-authority'
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { LoaderCircle, Network, X } from 'lucide-react'

export function cn(...inputs: ClassValue[]) { return twMerge(clsx(inputs)) }
const buttonStyles = cva('button', { variants: { variant: { primary: 'button-primary', secondary: 'button-secondary', ghost: 'button-ghost', danger: 'button-danger' }, size: { default: '', small: 'button-small', icon: 'button-icon' } }, defaultVariants: { variant: 'secondary', size: 'default' } })
export function Button({ variant, size, busy, className, children, disabled, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'secondary' | 'ghost' | 'danger'; size?: 'default' | 'small' | 'icon'; busy?: boolean }) {
  return <button className={cn(buttonStyles({ variant, size }), className)} disabled={disabled || busy} {...props}>{busy && <LoaderCircle size={15} className="spin" />}{children}</button>
}
export function Brand({ compact = false }: { compact?: boolean }) { return <div className="brand"><span className="brand-mark"><Network size={21} strokeWidth={2} /></span>{!compact && <span>Omni<span className="brand-light">Harness</span></span>}</div> }
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: 'neutral' | 'blue' | 'green' | 'amber' | 'red' }) { return <span className={`badge badge-${tone}`}>{children}</span> }
export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const sectionRef = useRef<HTMLElement>(null)
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null
    return () => { before?.focus() }
  }, [])
  return createPortal(<div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose() }}><section ref={sectionRef} className="modal" role="dialog" aria-modal="true" aria-label={title} onKeyDown={event => {
    if (event.key === 'Escape') onClose()
    if (event.key !== 'Tab') return
    const elements = Array.from(sectionRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]') ?? []).filter(element => element.offsetParent !== null)
    const first = elements[0], last = elements.at(-1)
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
  }}><div className="modal-heading"><h2>{title}</h2><Button size="icon" variant="ghost" aria-label="关闭" onClick={onClose}><X size={19} /></Button></div>{children}</section></div>, document.body)
}

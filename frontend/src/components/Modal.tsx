import { useEffect, useId, useRef } from 'react'
import type { ReactNode } from 'react'
import Icon from './Icon'

export default function Modal({ title, children, footer, onClose, busy = false }: {title: string; children: ReactNode; footer?: ReactNode; onClose: () => void; busy?: boolean}) {
  const id = useId()
  const dialog = useRef<HTMLDivElement>(null)
  const closeRef = useRef(onClose)
  const busyRef = useRef(busy)
  closeRef.current = onClose
  busyRef.current = busy
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const originalOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialog.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus()
    const onKey = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[role="dialog"]')
      if (dialogs[dialogs.length - 1] !== dialog.current) return
      if (event.key === 'Escape' && !busyRef.current) {event.preventDefault(); closeRef.current()}
      if (event.key !== 'Tab') return
      const focusable = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]') ?? []).filter(el => el.getClientRects().length > 0)
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (!first) {event.preventDefault(); dialog.current?.focus(); return}
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) {event.preventDefault(); last.focus()}
      else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus()}
    }
    document.addEventListener('keydown', onKey)
    return () => {document.removeEventListener('keydown', onKey); document.body.style.overflow = originalOverflow; previous?.focus()}
  }, [])
  return <div className="modal-mask" onMouseDown={e => {if (e.target === e.currentTarget && !busy) onClose()}}>
    <div className="modal" ref={dialog} role="dialog" aria-modal="true" aria-labelledby={id} tabIndex={-1}>
      <div className="modal-header"><h3 id={id}>{title}</h3><button className="icon-button" type="button" onClick={onClose} disabled={busy} aria-label="关闭弹窗"><Icon name="close"/></button></div>
      <div className="modal-body">{children}</div>
      {footer && <div className="modal-footer">{footer}</div>}
    </div>
  </div>
}

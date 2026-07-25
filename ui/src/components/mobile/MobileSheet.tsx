import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'

// Bottom-sheet shell shared by every mobile overlay: dimmed backdrop
// (tap to close), rounded top panel with a grab handle, safe-area
// bottom padding. Content scrolls internally; the sheet itself is fixed.
// Visibility is the parent's job — conditionally mount the sheet
// (`{open && <MobileSheet …>}`), there is no `open` prop.
export function MobileSheet({
  onClose,
  heightClass,
  children,
}: {
  onClose: () => void
  // Tailwind height class for the panel, e.g. 'h-[72vh]'. Omit to let
  // content size the sheet (capped at 85vh).
  heightClass?: string
  children: React.ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onClose])

  return createPortal(
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 bg-black/45" onClick={onClose} />
      <div
        className={cn(
          'absolute inset-x-0 bottom-0 flex max-h-[85vh] flex-col rounded-t-2xl border-t border-border bg-background pb-[env(safe-area-inset-bottom)] shadow-2xl',
          heightClass,
        )}
      >
        <button
          onClick={onClose}
          aria-label="Close"
          className="flex shrink-0 justify-center pb-1 pt-2.5"
        >
          <span className="h-1.5 w-10 rounded-full bg-muted-foreground/40" />
        </button>
        {children}
      </div>
    </div>,
    document.body,
  )
}

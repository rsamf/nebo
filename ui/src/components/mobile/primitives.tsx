import { cn } from '@/lib/utils'

// Shared mobile chrome: the pill chip, the segmented control and the
// round icon-button class each appeared in three-plus hand-rolled copies
// (with drifting paddings/alphas) before being centralized here.

// Round tappable icon button (headers, sheets, canvas controls). Callers
// append background/text-color classes.
export const MOBILE_ICON_BUTTON_CLASS =
  'flex h-9 w-9 shrink-0 items-center justify-center rounded-full'

// Log/alert severity filter vocabulary, shared by the feed's log cards
// and the alerts sheet.
export const LEVEL_FILTERS = ['All', 'Info', 'Warn', 'Error'] as const
export type LevelFilter = (typeof LEVEL_FILTERS)[number]

/** Pill filter chip (stage rail, log levels, alert severities, modality
 *  toggles). `leading` renders a small adornment such as a color dot. */
export function Chip({
  label,
  active,
  onTap,
  leading,
  className,
}: {
  label: string
  active: boolean
  onTap: () => void
  leading?: React.ReactNode
  className?: string
}) {
  return (
    <button
      onClick={onTap}
      className={cn(
        'flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1 text-xs font-medium',
        active
          ? 'border-primary/40 bg-primary/15 text-foreground'
          : 'border-border text-muted-foreground',
        className,
      )}
    >
      {leading}
      {label}
    </button>
  )
}

/** Equal-width segmented control (feed type filter, tracker step/time). */
export function Segmented<T extends string>({
  options,
  value,
  onChange,
  className,
}: {
  options: readonly { value: T; label: React.ReactNode }[]
  value: T
  onChange: (v: T) => void
  className?: string
}) {
  return (
    <div className={cn('flex gap-0.5 rounded-[9px] bg-muted p-0.5', className)}>
      {options.map(o => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={cn(
            'flex-1 rounded-[7px] px-3 py-1 text-center text-xs font-medium',
            value === o.value ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground',
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

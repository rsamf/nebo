import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { formatConfigValue } from '@/lib/utils'

interface ConfigChipsProps {
  params: Record<string, unknown>
  className?: string
  maxVisible?: number
}

export function ConfigChips({ params, className, maxVisible = 3 }: ConfigChipsProps) {
  const entries = Object.entries(params)
  if (entries.length === 0) return null
  const visible = entries.slice(0, maxVisible)
  const overflow = entries.length - visible.length

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          onClick={e => e.stopPropagation()}
          className={`mt-1.5 flex flex-wrap gap-1 text-left hover:opacity-80 transition-opacity ${className ?? ''}`}
        >
          {visible.map(([k, v]) => (
            <span
              key={k}
              className="text-[10px] bg-muted px-1.5 py-0.5 rounded text-muted-foreground max-w-[140px] truncate"
              title={`${k}: ${formatConfigValue(v)}`}
            >
              {k}: {formatConfigValue(v)}
            </span>
          ))}
          {overflow > 0 && (
            <span className="text-[10px] text-muted-foreground self-center">+{overflow}</span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-80 p-3"
        onClick={e => e.stopPropagation()}
      >
        <div className="text-xs font-medium mb-2">Config</div>
        <div className="space-y-1 font-mono text-xs max-h-[50vh] overflow-auto">
          {entries.map(([k, v]) => (
            <div key={k} className="flex justify-between gap-3">
              <span className="text-muted-foreground shrink-0">{k}</span>
              <span className="text-foreground break-all text-right">{formatConfigValue(v)}</span>
            </div>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  )
}

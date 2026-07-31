import { useStore } from '@/store'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

interface RunHoverInfoProps {
  runId: string
  side?: 'top' | 'right' | 'bottom' | 'left'
  children: React.ReactNode
}

/**
 * Hover card with a run's vitals: run id and start time. Wraps its child as
 * the hover trigger (the whole run row, the run-id chip in the header, ...).
 * Node/metric counts are deliberately absent — they read 0 for runs the
 * daemon hasn't deep-ingested yet, so showing them here would be misleading.
 */
export function RunHoverInfo({ runId, side = 'right', children }: RunHoverInfoProps) {
  const started = useStore(s => s.runs.get(runId)?.summary.started_at)

  const startedAt = started
    ? new Date(started).toLocaleString(undefined, {
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
      })
    : null

  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} align="start">
        <div className="font-mono">{runId}</div>
        {startedAt && <div className="text-muted-foreground mt-1">{startedAt}</div>}
      </TooltipContent>
    </Tooltip>
  )
}

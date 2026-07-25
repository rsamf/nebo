import { useEffect, useMemo, useState } from 'react'
import { useStore } from '@/store'
import { api } from '@/lib/api'
import { MobileSheet } from './MobileSheet'
import {
  ALERT_SEVERITY_COLOR, alertSeverity, loggableDisplayName, timeAgo,
} from './util'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

const SEVERITY_FILTERS = ['All', 'Info', 'Warn', 'Error'] as const
type SeverityFilter = (typeof SEVERITY_FILTERS)[number]

// Severity-filterable list of this run's fired alerts. Hydrated from
// GET /runs/{id}/alerts on open; live alert events append via the store.
export function MobileAlertsSheet({
  runId,
  onClose,
  onOpenNode,
}: {
  runId: string
  onClose: () => void
  onOpenNode: (loggableId: string) => void
}) {
  const run = useStore(s => s.runs).get(runId)
  const setRunAlerts = useStore(s => s.setRunAlerts)
  const [filter, setFilter] = useState<SeverityFilter>('All')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let cancelled = false
    api
      .getRunAlerts(runId)
      .then(d => {
        if (cancelled) return
        setRunAlerts(runId, d.alerts)
        setLoaded(true)
      })
      .catch(() => setLoaded(true))
    return () => {
      cancelled = true
    }
  }, [runId, setRunAlerts])

  const alerts = useMemo(() => {
    const all = run?.alerts ?? []
    const filtered =
      filter === 'All' ? all : all.filter(a => alertSeverity(a.level) === filter.toLowerCase())
    // Newest first.
    return filtered.slice().sort((a, b) => b.timestamp - a.timestamp)
  }, [run?.alerts, filter])

  return (
    <MobileSheet open onClose={onClose} heightClass="h-[66vh]">
      <div className="flex shrink-0 items-center gap-2 px-4 pb-2.5">
        <span className="flex-1 text-base font-semibold">Alerts</span>
      </div>
      <div className="flex shrink-0 gap-1.5 px-4 pb-2.5">
        {SEVERITY_FILTERS.map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              'rounded-full border px-2.5 py-0.5 text-[10.5px] font-medium',
              filter === f
                ? 'border-primary/40 bg-primary/15 text-foreground'
                : 'border-border text-muted-foreground',
            )}
          >
            {f}
          </button>
        ))}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 pb-10">
        {alerts.length === 0 && (
          <div className="py-8 text-center text-xs text-muted-foreground">
            {loaded ? 'No alerts fired on this run' : 'Loading…'}
          </div>
        )}
        {alerts.map((a, i) => {
          const severity = alertSeverity(a.level)
          const dot = ALERT_SEVERITY_COLOR[severity]
          const metaParts = [
            a.level_name || severity.toUpperCase(),
            timeAgo(a.timestamp),
          ]
          if (a.condition) metaParts.push(a.condition)
          const target = a.loggable_id
          return (
            <button
              key={`${a.timestamp}-${i}`}
              onClick={() => {
                if (target) {
                  onClose()
                  onOpenNode(target)
                }
              }}
              className={cn(
                'flex gap-3 rounded-xl border bg-card px-3.5 py-3 text-left',
                severity === 'error' ? 'border-red-500/40' : 'border-border',
                !target && 'cursor-default',
              )}
            >
              <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: dot }} />
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium leading-snug">{a.title}</div>
                {a.text && (
                  <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground">{a.text}</div>
                )}
                <div className="mt-1 truncate font-mono text-[11px] text-muted-foreground">
                  {metaParts.join(' · ')}
                  {target ? ` · ${loggableDisplayName(run, target)}` : ''}
                </div>
              </div>
              {target && <ChevronRight className="mt-1 h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
            </button>
          )
        })}
      </div>
    </MobileSheet>
  )
}

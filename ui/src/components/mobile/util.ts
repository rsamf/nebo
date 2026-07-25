// Shared helpers for the mobile experience.
import type { RunState } from '@/store'
import type { MetricEntry, MetricType } from '@/lib/api'

// One card recipe for every tappable row on the mobile list screens
// (runs, groups, the group page's Notes button) — identical fixed height
// so mixed lists read as one rhythm.
export const MOBILE_CARD_CLASS =
  'mb-2 flex h-16 w-full items-center gap-3 rounded-xl border border-border bg-card px-3.5 text-left'

/** Shortened run id for compact metadata rows. */
export function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id
}

/** Compact relative time since an epoch-seconds timestamp. */
export function timeAgo(epochSeconds: number | null | undefined): string {
  if (typeof epochSeconds !== 'number') return ''
  const s = Math.max(0, Date.now() / 1000 - epochSeconds)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

/** Elapsed-time label for the tracker's time mode ("1m32s"). */
export function elapsedLabel(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

// Both sparkline helpers take the loggableMetrics record (not the run):
// the store mutates runs in place, so callers must key re-computation on
// the replaced field refs rather than the stable run object.

/** Numeric values of the run's first accumulating line series — the
 *  sparkline shown on run cards. Null when nothing suitable is loaded. */
export function firstLineSeriesValues(
  loggableMetrics: RunState['loggableMetrics'] | undefined, max = 80,
): number[] | null {
  if (!loggableMetrics) return null
  for (const byName of Object.values(loggableMetrics)) {
    const values = lineValuesIn(byName, max)
    if (values) return values
  }
  return null
}

/** A loggable's first metric series of any chart type — the DAG node
 *  card preview (rendered via MetricPreview). */
export function firstSeriesFor(
  loggableMetrics: RunState['loggableMetrics'] | undefined, loggableId: string,
): { type: MetricType; entries: MetricEntry[] } | null {
  const byName = loggableMetrics?.[loggableId]
  if (!byName) return null
  for (const series of Object.values(byName)) {
    if (series.entries.length > 0) return series
  }
  return null
}

function lineValuesIn(
  byName: Record<string, { type: string; entries: MetricEntry[] }>, max: number,
): number[] | null {
  for (const series of Object.values(byName)) {
    if (series.type !== 'line') continue
    const values = series.entries
      .slice(-max)
      .map(e => (typeof e.value === 'number' ? e.value : NaN))
      .filter(v => Number.isFinite(v))
    if (values.length >= 2) return values
  }
  return null
}

/** The entry at (or nearest below) the current playhead step. Stepless
 *  entries count as step 0; a null playhead also means step 0 — the DAG
 *  preview convention. Falls back to the earliest entry when everything
 *  is past the target. */
export function nearestAtStep<T extends { step: number | null }>(
  entries: T[] | undefined,
  step: number | null,
): T | null {
  if (!entries || entries.length === 0) return null
  const target = step ?? 0
  let best: T | null = null
  let bestStep = -Infinity
  for (const e of entries) {
    const s = e.step ?? 0
    if (s <= target && s >= bestStep) {
      best = e
      bestStep = s
    }
  }
  return best ?? entries[0]
}

/** Latest displayable value for a metric series, used on feed cards. */
export function latestMetricLabel(series: { type: string; entries: MetricEntry[] }): string {
  const last = series.entries[series.entries.length - 1]
  if (!last) return ''
  if (series.type === 'line') return formatNumber(last.value)
  if (series.type === 'scatter') {
    let n = 0
    for (const e of series.entries) {
      const v = e.value
      if (v && typeof v === 'object') {
        for (const pts of Object.values(v as Record<string, { x?: unknown[] }>)) {
          n += Array.isArray(pts) ? pts.length : (pts?.x?.length ?? 0)
        }
      }
    }
    return `${n} pts`
  }
  const v = last.value
  if (v && typeof v === 'object') {
    const n = Object.keys(v as Record<string, unknown>).length
    return `${n} ${series.type === 'histogram' ? (n === 1 ? 'label' : 'labels') : (n === 1 ? 'category' : 'categories')}`
  }
  return formatNumber(v)
}

export function formatNumber(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return String(v ?? '')
  const abs = Math.abs(v)
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (abs >= 1e4) return `${(v / 1e3).toFixed(1)}k`
  if (abs >= 100) return v.toFixed(1)
  if (abs >= 1) return v.toFixed(3)
  if (abs === 0) return '0'
  if (abs < 1e-3) return v.toExponential(2)
  return v.toFixed(4)
}

/** Alert severity bucket from the numeric level. */
export function alertSeverity(level: number): 'error' | 'warn' | 'info' {
  if (level >= 40) return 'error'
  if (level >= 30) return 'warn'
  return 'info'
}

export const ALERT_SEVERITY_COLOR: Record<'error' | 'warn' | 'info', string> = {
  error: '#f87171',
  warn: '#fbbf24',
  info: '#60a5fa',
}

/** Display name for a loggable id: node name, or the global/agent labels. */
export function loggableDisplayName(run: RunState | undefined, loggableId: string): string {
  const node = run?.graph?.nodes[loggableId]
  if (node) return node.name
  if (loggableId === run?.globalLoggable?.loggableId || loggableId === '__global__') return 'global'
  if (loggableId === run?.agentLoggable?.loggableId || loggableId === '__agent__') return 'agent'
  return loggableId
}


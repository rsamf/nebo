import { useEffect, useMemo, useRef, useState } from 'react'
import { Chart } from 'chart.js'
import { useStore } from '@/store'
import { useStreams } from '@/hooks/useStreams'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { withAlpha } from '@/components/charts/withAlpha'
import type { StreamLeaf, StreamModality } from '@/lib/streams'
import { MobileSheet } from './MobileSheet'
import { elapsedLabel } from './util'
import { ChevronLeft, ChevronRight, ChevronUp, GitBranch, Rows3 } from 'lucide-react'
import { cn } from '@/lib/utils'

// Persistent bottom tracker: an event-activity overview (Chart.js area
// sparkline in the run color) with the DAG ⇄ Feed view toggle in the
// collapsed bar; tapping the strip expands a sheet with per-stream dot
// rows and a large scrubber.

const HEAT_BUCKETS = 64
const MODALITIES: { key: StreamModality; label: string; color: string }[] = [
  { key: 'text', label: 'text', color: '#60a5fa' },
  { key: 'image', label: 'image', color: '#34d399' },
  { key: 'audio', label: 'audio', color: '#fbbf24' },
]
const MODALITY_COLOR: Record<StreamModality, string> = {
  text: '#60a5fa', image: '#34d399', audio: '#fbbf24',
}

// Chart.js area sparkline of event density for the collapsed bar. The
// canvas mounts exactly once (project invariant: never remount a canvas
// under a live Chart instance) and data flows in via chart.update().
function ActivityChart({ counts, color }: { counts: number[]; color: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const chartRef = useRef<Chart<'line', number[], number> | null>(null)

  // sqrt-compress so a single dense burst doesn't flatten the rest.
  const data = useMemo(() => counts.map(c => Math.sqrt(c)), [counts])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const chart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: data.map((_, i) => i),
        datasets: [{
          data,
          borderColor: color,
          backgroundColor: withAlpha(color, 0.18),
          borderWidth: 1.5,
          pointRadius: 0,
          fill: true,
          tension: 0.35,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        events: [],
        plugins: { tooltip: { enabled: false }, legend: { display: false } },
        scales: {
          x: { display: false },
          // Headroom above the peak so uniform activity draws as a line
          // with air over it instead of a solid block.
          y: { display: false, beginAtZero: true, suggestedMax: Math.max(1, ...data) * 1.4 },
        },
        layout: { padding: 0 },
      },
    })
    chartRef.current = chart
    return () => {
      chart.destroy()
      chartRef.current = null
    }
    // Mount-once by design; data and color updates flow through the
    // effects below without touching the canvas.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    chart.data.labels = data.map((_, i) => i)
    chart.data.datasets[0].data = data
    chart.data.datasets[0].borderColor = color
    chart.data.datasets[0].backgroundColor = withAlpha(color, 0.18)
    if (chart.options.scales?.y) {
      chart.options.scales.y.suggestedMax = Math.max(1, ...data) * 1.4
    }
    chart.update('none')
  }, [data, color])

  return <canvas ref={canvasRef} className="pointer-events-none" />
}

function DotRow({
  leaf,
  isStep,
  min,
  range,
  scrubPct,
}: {
  leaf: StreamLeaf
  isStep: boolean
  min: number
  range: number
  scrubPct: number
}) {
  // Dedupe datapoints to one dot per ~0.5% of track width — dense streams
  // would otherwise render thousands of coincident spans.
  const pcts = useMemo(() => {
    const seen = new Set<number>()
    const out: number[] = []
    for (const d of leaf.datapoints) {
      const v = isStep ? d.step : d.timestamp
      if (v == null) continue
      const pct = ((v - min) / range) * 100
      const bucket = Math.round(pct * 2)
      if (seen.has(bucket)) continue
      seen.add(bucket)
      out.push(pct)
    }
    return out
  }, [leaf.datapoints, isStep, min, range])

  return (
    <div className="relative h-[30px] border-b border-border/60">
      {pcts.map(pct => (
        <span
          key={pct}
          className="absolute top-3 h-1.5 w-1.5 -translate-x-1/2 rounded-full opacity-85"
          style={{ left: `${pct}%`, background: MODALITY_COLOR[leaf.modality] }}
        />
      ))}
      <span className="absolute left-0.5 top-0 max-w-[70%] truncate text-[11px] leading-[30px] text-foreground/85 [text-shadow:0_0_4px_var(--color-background),0_0_4px_var(--color-background)]">
        {leaf.path}
      </span>
      <span
        className="absolute bottom-0 top-0 w-0.5 -translate-x-1/2 bg-foreground"
        style={{ left: `${scrubPct}%` }}
      />
    </div>
  )
}

export function MobileTracker({ runId }: { runId: string }) {
  const timeline = useStore(s => s.timeline)
  const setStep = useStore(s => s.setTimelineStep)
  const setTime = useStore(s => s.setTimelineTime)
  const setMode = useStore(s => s.setTimelineMode)
  const viewMode = useStore(s => s.viewMode)
  const setViewMode = useStore(s => s.setViewMode)
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR

  const [open, setOpen] = useState(false)
  const [activeModalities, setActiveModalities] = useState<Set<StreamModality>>(
    () => new Set(MODALITIES.map(m => m.key)),
  )

  const isStep = timeline.mode === 'step'
  const model = useStreams(runId, true)
  const loggableMetrics = useStore(s => s.runs.get(runId)?.loggableMetrics)

  const leaves = useMemo(
    () => model.leaves.filter(l => activeModalities.has(l.modality)),
    [model.leaves, activeModalities],
  )

  // Metric emissions count toward the overview's domain and density —
  // streams alone would leave a metrics-only run with an empty strip and
  // a playhead that never tracks steps committed from the charts.
  const metricPoints = useMemo(() => {
    const pts: { step: number | null; timestamp: number }[] = []
    for (const byName of Object.values(loggableMetrics ?? {})) {
      for (const series of Object.values(byName)) {
        if (series.type !== 'line' && series.type !== 'scatter') continue
        for (const e of series.entries) pts.push({ step: e.step, timestamp: e.timestamp })
      }
    }
    return pts
  }, [loggableMetrics])

  const [min, max] = useMemo(() => {
    let lo = Infinity
    let hi = -Infinity
    for (const l of leaves) {
      if (isStep) {
        if (l.minStep != null) lo = Math.min(lo, l.minStep)
        if (l.maxStep != null) hi = Math.max(hi, l.maxStep)
      } else {
        lo = Math.min(lo, l.minTime)
        hi = Math.max(hi, l.maxTime)
      }
    }
    for (const p of metricPoints) {
      const v = isStep ? p.step : p.timestamp
      if (v == null) continue
      if (v < lo) lo = v
      if (v > hi) hi = v
    }
    if (lo === Infinity) {
      lo = 0
      hi = 0
    }
    return [lo, hi]
  }, [leaves, metricPoints, isStep])
  const range = max - min

  // Event-density buckets for the activity chart.
  const heat = useMemo(() => {
    const counts = new Array<number>(HEAT_BUCKETS).fill(0)
    if (range <= 0) return counts
    const bump = (v: number | null) => {
      if (v == null) return
      const idx = Math.min(HEAT_BUCKETS - 1, Math.max(0, Math.floor(((v - min) / range) * HEAT_BUCKETS)))
      counts[idx]++
    }
    for (const l of leaves) {
      for (const d of l.datapoints) bump(isStep ? d.step : d.timestamp)
    }
    for (const p of metricPoints) bump(isStep ? p.step : p.timestamp)
    return counts
  }, [leaves, metricPoints, isStep, min, range])

  const playhead = isStep ? timeline.step : timeline.time
  const effective = playhead ?? max
  const scrubPct = range > 0 ? Math.max(0, Math.min(100, ((effective - min) / range) * 100)) : 100

  const posLabel = isStep ? `step ${Math.round(effective)}` : `+${elapsedLabel(effective - min)}`

  const commit = (v: number) => {
    if (isStep) setStep(Math.round(v))
    else setTime(v)
  }
  const nudge = (dir: 1 | -1) => {
    if (range <= 0) return
    const delta = isStep ? 1 : range / 100
    commit(Math.max(min, Math.min(max, effective + dir * delta)))
  }

  const filtering = playhead != null

  return (
    <>
      <div className="shrink-0 border-t border-border bg-muted/30 px-4 pb-[max(env(safe-area-inset-bottom),10px)] pt-2.5">
        <div className="flex items-center gap-3">
          <div className="flex shrink-0 rounded-full bg-muted p-0.5">
            <button
              onClick={() => setViewMode('graph')}
              aria-label="DAG view"
              className={cn(
                'flex h-7 w-9 items-center justify-center rounded-full',
                viewMode === 'graph' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground',
              )}
            >
              <GitBranch className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => setViewMode('flat')}
              aria-label="Feed view"
              className={cn(
                'flex h-7 w-9 items-center justify-center rounded-full',
                viewMode === 'flat' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground',
              )}
            >
              <Rows3 className="h-3.5 w-3.5" />
            </button>
          </div>
          <button
            onClick={() => setOpen(true)}
            className="flex min-w-0 flex-1 items-center gap-2.5 py-1.5"
            aria-label="Open timeline"
          >
            <div className="relative h-5 min-w-0 flex-1">
              <ActivityChart counts={heat} color={runColor} />
              <span
                className="absolute inset-y-0 w-px -translate-x-1/2 bg-foreground/50"
                style={{ left: `${scrubPct}%` }}
              />
            </div>
            {range > 0 && (
              <span className={cn('shrink-0 text-[11px] tabular-nums', filtering ? 'text-foreground' : 'text-muted-foreground')}>
                {posLabel}
              </span>
            )}
            <ChevronUp className="h-3 w-3 shrink-0 text-muted-foreground" />
          </button>
        </div>
      </div>

      <MobileSheet open={open} onClose={() => setOpen(false)}>
        <div className="px-4 pb-6">
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-baseline gap-2.5">
              <span className="text-[15px] font-semibold">Timeline</span>
              {filtering && (
                <button
                  onClick={() => {
                    setStep(null)
                    setTime(null)
                  }}
                  className="text-[11px] text-muted-foreground underline underline-offset-2"
                >
                  clear
                </button>
              )}
            </div>
            <div className="flex rounded-lg bg-muted p-0.5">
              {(['step', 'time'] as const).map(m => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn(
                    'rounded-md px-3 py-0.5 text-[11px] font-medium capitalize',
                    timeline.mode === m ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground',
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>

          <div className="mb-3.5 flex gap-1.5">
            {MODALITIES.map(m => {
              const active = activeModalities.has(m.key)
              return (
                <button
                  key={m.key}
                  onClick={() =>
                    setActiveModalities(prev => {
                      const next = new Set(prev)
                      if (next.has(m.key)) next.delete(m.key)
                      else next.add(m.key)
                      return next
                    })
                  }
                  className={cn(
                    'flex items-center gap-1.5 rounded-full border px-3 py-1 text-[11px] font-medium',
                    active
                      ? 'border-primary/40 bg-primary/10 text-foreground'
                      : 'border-border text-muted-foreground',
                  )}
                >
                  <span className="h-1.5 w-1.5 rounded-full" style={{ background: active ? m.color : 'var(--color-muted-foreground)' }} />
                  {m.label}
                </button>
              )
            })}
          </div>

          {range <= 0 ? (
            <div className="py-6 text-center text-xs text-muted-foreground">
              {isStep ? 'No step data yet' : 'No time data yet'}
            </div>
          ) : (
            <>
              <div className="no-scrollbar mb-1.5 max-h-[32vh] overflow-y-auto">
                {leaves
                  .slice()
                  .sort((a, b) => a.path.localeCompare(b.path))
                  .map(leaf => (
                    <DotRow
                      key={leaf.path}
                      leaf={leaf}
                      isStep={isStep}
                      min={min}
                      range={range}
                      scrubPct={scrubPct}
                    />
                  ))}
              </div>

              <input
                type="range"
                min={min}
                max={max}
                step={isStep ? 1 : range / 500 || 1}
                value={effective}
                onInput={e => commit(Number((e.target as HTMLInputElement).value))}
                className="my-1 h-9 w-full accent-primary"
                aria-label="Scrub timeline"
              />
              <div className="flex items-center justify-center gap-3.5">
                <button
                  onClick={() => nudge(-1)}
                  aria-label={isStep ? 'Previous step' : 'Back'}
                  className="flex h-11 w-11 items-center justify-center rounded-full bg-muted"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <span className="min-w-[110px] text-center text-base font-semibold tabular-nums">
                  {isStep
                    ? `${Math.round(effective)} / ${Math.round(max)}`
                    : `${elapsedLabel(effective - min)} / ${elapsedLabel(range)}`}
                </span>
                <button
                  onClick={() => nudge(1)}
                  aria-label={isStep ? 'Next step' : 'Forward'}
                  className="flex h-11 w-11 items-center justify-center rounded-full bg-muted"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
            </>
          )}
        </div>
      </MobileSheet>
    </>
  )
}

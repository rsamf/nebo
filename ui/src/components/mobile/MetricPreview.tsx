import { useMemo } from 'react'
import type { LoggableMetricSeries, MetricEntry } from '@/lib/api'
import { RUN_COLOR_PALETTE } from '@/lib/colors'
import { emaSmooth } from '@/components/charts/smoothing'
import { Sparkline } from './Sparkline'

// Dependency-free mini plot of a metric series — the collapsed-card /
// DAG-node preview for every chart type. Chart.js is deliberately
// avoided: dozens of these render in scrolling lists.
export function MetricPreview({
  series,
  color,
  width = 88,
  height = 26,
  smoothing = 0,
  className,
}: {
  series: LoggableMetricSeries
  color: string
  width?: number
  height?: number
  // EMA factor applied to line previews (the global lineSmoothing knob).
  smoothing?: number
  className?: string
}) {
  const latest = series.entries[series.entries.length - 1]

  const lineValues = useMemo(() => {
    if (series.type !== 'line') return null
    const values = series.entries
      .slice(-80)
      .map(e => (typeof e.value === 'number' ? e.value : NaN))
      .filter(v => Number.isFinite(v))
    if (values.length < 2) return null
    return emaSmooth(values, smoothing)
  }, [series.type, series.entries, smoothing])

  const scatterPts = useMemo(
    () => (series.type === 'scatter' ? scatterPoints(series.entries) : null),
    [series.type, series.entries],
  )

  if (series.type === 'line') {
    return lineValues ? (
      <Sparkline values={lineValues} color={color} width={width} height={height} className={className} />
    ) : null
  }
  if (!latest) return null

  if (series.type === 'bar') {
    const values = numericValues(latest).slice(0, 12)
    if (values.length === 0) return null
    const max = Math.max(...values.map(Math.abs), 1e-9)
    const n = values.length
    const slot = 100 / n
    const barW = Math.max(2, slot * 0.7)
    return (
      <svg width={width} height={height} viewBox="0 0 100 28" preserveAspectRatio="none" className={className} aria-hidden>
        {values.map((v, i) => {
          const h = Math.max(2, (Math.abs(v) / max) * 26)
          return (
            <rect
              key={i}
              x={i * slot + (slot - barW) / 2}
              y={28 - h}
              width={barW}
              height={h}
              fill={color}
              opacity={0.85}
            />
          )
        })}
      </svg>
    )
  }

  if (series.type === 'pie') {
    const values = numericValues(latest).filter(v => v > 0).slice(0, 8)
    if (values.length === 0) return null
    // A single slice degenerates as an arc path — draw a full circle.
    if (values.length === 1) {
      return (
        <svg width={height} height={height} viewBox="0 0 28 28" className={className} aria-hidden>
          <circle cx={14} cy={14} r={12} fill={RUN_COLOR_PALETTE[0]} />
        </svg>
      )
    }
    const total = values.reduce((a, b) => a + b, 0)
    const r = 12
    const arcs: string[] = []
    let angle = -Math.PI / 2
    for (const v of values) {
      const sweep = (v / total) * Math.PI * 2
      const x1 = 14 + r * Math.cos(angle)
      const y1 = 14 + r * Math.sin(angle)
      angle += sweep
      const x2 = 14 + r * Math.cos(angle)
      const y2 = 14 + r * Math.sin(angle)
      const large = sweep > Math.PI ? 1 : 0
      arcs.push(`M 14 14 L ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2} Z`)
    }
    return (
      <svg width={height} height={height} viewBox="0 0 28 28" className={className} aria-hidden>
        {arcs.map((d, i) => (
          <path key={i} d={d} fill={RUN_COLOR_PALETTE[i % RUN_COLOR_PALETTE.length]} />
        ))}
      </svg>
    )
  }

  if (series.type === 'scatter') {
    if (!scatterPts || scatterPts.length === 0) return null
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    for (const p of scatterPts) {
      if (p.x < minX) minX = p.x
      if (p.x > maxX) maxX = p.x
      if (p.y < minY) minY = p.y
      if (p.y > maxY) maxY = p.y
    }
    const rx = maxX - minX || 1
    const ry = maxY - minY || 1
    return (
      <svg width={width} height={height} viewBox="0 0 100 28" preserveAspectRatio="none" className={className} aria-hidden>
        {scatterPts.map((p, i) => (
          <circle
            key={i}
            cx={2 + ((p.x - minX) / rx) * 96}
            cy={26 - ((p.y - minY) / ry) * 24}
            r={1.3}
            fill={color}
            opacity={0.75}
          />
        ))}
      </svg>
    )
  }

  if (series.type === 'histogram') {
    const samples: number[] = []
    const v = latest.value
    if (v && typeof v === 'object') {
      for (const arr of Object.values(v as Record<string, unknown>)) {
        if (Array.isArray(arr)) for (const s of arr) if (typeof s === 'number' && Number.isFinite(s)) samples.push(s)
      }
    }
    if (samples.length === 0) return null
    const BINS = 16
    let min = Infinity, max = -Infinity
    for (const s of samples) {
      if (s < min) min = s
      if (s > max) max = s
    }
    const range = max - min || 1
    const counts = new Array<number>(BINS).fill(0)
    for (const s of samples) {
      counts[Math.min(BINS - 1, Math.floor(((s - min) / range) * BINS))]++
    }
    const peak = Math.max(...counts, 1)
    const slot = 100 / BINS
    return (
      <svg width={width} height={height} viewBox="0 0 100 28" preserveAspectRatio="none" className={className} aria-hidden>
        {counts.map((c, i) => {
          const h = c === 0 ? 0 : Math.max(1.5, (c / peak) * 26)
          return h > 0 ? (
            <rect key={i} x={i * slot + slot * 0.1} y={28 - h} width={slot * 0.8} height={h} fill={color} opacity={0.85} />
          ) : null
        })}
      </svg>
    )
  }

  return null
}

function numericValues(entry: MetricEntry): number[] {
  const v = entry.value
  if (!v || typeof v !== 'object') return []
  return Object.values(v as Record<string, unknown>).filter(
    (x): x is number => typeof x === 'number' && Number.isFinite(x),
  )
}

const MAX_PREVIEW_POINTS = 90

// Union of scatter points across emissions; the on-wire/store shape is
// {label: {x: [...], y: [...]}} (see ScatterMetric, which enforces the
// same shape). Two passes: count first, then allocate only the ~90
// stride-sampled points — accumulating scatters can hold thousands.
function scatterPoints(entries: MetricEntry[]): { x: number; y: number }[] {
  const eachSlot = (fn: (xs: unknown[], ys: unknown[]) => void) => {
    for (const e of entries) {
      const v = e.value
      if (!v || typeof v !== 'object') continue
      for (const val of Object.values(v as Record<string, unknown>)) {
        if (!val || typeof val !== 'object') continue
        const xs = (val as { x?: unknown }).x
        const ys = (val as { y?: unknown }).y
        if (Array.isArray(xs) && Array.isArray(ys)) fn(xs, ys)
      }
    }
  }
  let total = 0
  eachSlot(xs => {
    total += xs.length
  })
  if (total === 0) return []
  const stride = Math.max(1, Math.ceil(total / MAX_PREVIEW_POINTS))
  const pts: { x: number; y: number }[] = []
  let i = 0
  eachSlot((xs, ys) => {
    const n = Math.min(xs.length, ys.length)
    for (let j = 0; j < n; j++, i++) {
      if (i % stride === 0 && typeof xs[j] === 'number' && typeof ys[j] === 'number') {
        pts.push({ x: xs[j] as number, y: ys[j] as number })
      }
    }
  })
  return pts
}

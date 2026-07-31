import { useMemo } from 'react'
import type { AxisTransform } from '@/hooks/useAxisTransform'
import { MODALITY_COLORS as DOT_COLOR, type FlatRow, type StreamModality } from '@/lib/streams'

// Dedupe a row's datapoints by quantized x so DOM node count is bounded by
// track width (~2.5px buckets at zoom 1), not by datapoint count. Bucket
// resolution scales with zoom, so points that collapse when zoomed out
// reappear as you zoom in.
function bucketRow(
  datapoints: { step: number | null; timestamp: number }[],
  isStep: boolean,
  toPercent: (v: number) => number,
  perPercent: number,
): number[] {
  const seen = new Set<number>()
  const out: number[] = []
  for (const dp of datapoints) {
    const v = isStep ? dp.step : dp.timestamp
    if (v == null) continue
    const pct = toPercent(v)
    const key = Math.round(pct * perPercent)
    if (seen.has(key)) continue
    seen.add(key)
    out.push(pct)
  }
  return out
}

// Sticky ruler band: tick labels + the playhead (with a downward triangle
// handle). Rendered inside a `left:pad right:pad` plot box so the first/last
// tick aren't clipped; the scaled layer shares the canvas X transform.
export function TimelineRuler({ ticks, axis, isStep, minTime, height, pad, playheadPct }: {
  ticks: number[]
  axis: AxisTransform
  isStep: boolean
  minTime: number
  height: number
  pad: number
  playheadPct: number | null
}) {
  const fmt = (t: number) => {
    if (isStep) return String(t)
    const off = t - minTime
    return off < 60 ? `${off.toFixed(1)}s` : `${(off / 60).toFixed(1)}m`
  }
  return (
    <div className="sticky top-0 z-10 overflow-hidden border-b border-border bg-background" style={{ height }}>
      <div className="absolute inset-y-0" style={{ left: pad, right: pad }}>
        <div className="absolute inset-0" style={axis.innerStyle}>
          {ticks.map((t, i) => (
            <div key={i} className="absolute top-0 -translate-x-1/2 pt-0.5 text-[9px] text-muted-foreground" style={{ left: `${axis.toPercent(t)}%` }}>
              {fmt(t)}
            </div>
          ))}
          {playheadPct != null && (
            <>
              <div className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-primary" style={{ left: `${playheadPct}%` }} />
              <div
                className="absolute top-0 h-0 w-0 -translate-x-1/2 border-l-[5px] border-r-[5px] border-t-[7px] border-l-transparent border-r-transparent border-t-primary"
                style={{ left: `${playheadPct}%` }}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// The scrollable canvas: one datapoint row per FlatRow (expanded branch rows
// without their own stream are blank spacers so rows stay aligned 1:1 with
// the tree column; collapsed branch rows merge every descendant stream's
// datapoints, colored per modality), tick guides, and the playhead — all
// inside the same `left:pad right:pad` plot box as the ruler. SVG layers are
// pointer-transparent so the parent handles scrub/pan/zoom.
export function TimelineRows({ rows, rowHeight, isStep, axis, ticks, pad, playheadPct, showLabels = false, labelsDimmed = false }: {
  rows: FlatRow[]
  rowHeight: number
  isStep: boolean
  axis: AxisTransform
  ticks: number[]
  pad: number
  playheadPct: number | null
  // Mobile: no tree column, so each leaf's full path is shown left-aligned on
  // its own row, layered over the datapoints (and dimmed while the user is
  // touching the canvas so the data underneath stays visible).
  showLabels?: boolean
  labelsDimmed?: boolean
}) {
  const totalH = rows.length * rowHeight
  // ~4 buckets per percent ≈ one per 2.5px on a 1000px track at zoom 1.
  const perPercent = 4 * axis.scale
  // Per row: one bucket list per modality, so merged (collapsed-branch) rows
  // keep per-dot modality colors. Plain leaf rows have exactly one entry.
  const bucketedRows = useMemo(
    () => rows.map((row): { modality: StreamModality; pcts: number[] }[] | null => {
      const leaves = row.mergedLeaves ?? (row.leaf ? [row.leaf] : null)
      if (!leaves || leaves.length === 0) return null
      const byModality = new Map<StreamModality, { step: number | null; timestamp: number }[]>()
      for (const l of leaves) {
        const arr = byModality.get(l.modality)
        if (arr) arr.push(...l.datapoints)
        else byModality.set(l.modality, [...l.datapoints])
      }
      return [...byModality.entries()].map(([modality, dps]) => ({
        modality,
        pcts: bucketRow(dps, isStep, axis.toPercent, perPercent),
      }))
    }),
    [rows, isStep, axis.toPercent, perPercent],
  )
  return (
    <div className="relative" style={{ height: totalH }}>
      <div className="absolute inset-y-0" style={{ left: pad, right: pad }}>
        <div className="absolute inset-0" style={axis.innerStyle}>
          <svg className="pointer-events-none absolute inset-0 h-full w-full" preserveAspectRatio="none">
            {ticks.map((t, i) => (
              <line key={i} x1={`${axis.toPercent(t)}%`} x2={`${axis.toPercent(t)}%`} y1="0" y2="100%" stroke="currentColor" className="text-foreground/10" strokeWidth={1} strokeDasharray="4 4" />
            ))}
          </svg>
          <svg className="pointer-events-none absolute inset-0 h-full w-full overflow-visible" preserveAspectRatio="none">
            {bucketedRows.map((buckets, i) => buckets
              ? buckets.map(b =>
                  b.pcts.map((pct, j) => (
                    <circle key={`${b.modality}-${j}`} cx={`${pct}%`} cy={i * rowHeight + rowHeight / 2} r={2.5} fill={DOT_COLOR[b.modality]} opacity={0.85} />
                  )),
                )
              : null)}
          </svg>
          {playheadPct != null && (
            <div className="pointer-events-none absolute top-0 w-0.5 -translate-x-1/2 bg-primary" style={{ left: `${playheadPct}%`, height: totalH }} />
          )}
        </div>
      </div>
      {/* Flat stream-name labels (mobile) — left-aligned, NOT transformed, so
          they stay put while the canvas pans/zooms. */}
      {showLabels && (
        <div className="pointer-events-none absolute inset-0 transition-opacity duration-150" style={{ opacity: labelsDimmed ? 0.3 : 0.9 }}>
          {rows.map((row, i) => row.leaf ? (
            <div
              key={row.key}
              className="absolute whitespace-nowrap text-[11px] text-foreground"
              style={{ top: i * rowHeight, left: pad + 2, height: rowHeight, lineHeight: `${rowHeight}px`, textShadow: '0 0 4px var(--background), 0 0 4px var(--background)' }}
            >
              {row.path}
            </div>
          ) : null)}
        </div>
      )}
    </div>
  )
}

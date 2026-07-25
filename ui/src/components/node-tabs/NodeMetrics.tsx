// Renders a single loggable's metrics tab. Works for node- and global-kind
// loggables, for a single run, and in multi-run comparison.
//
// Color conventions:
//   - In a single-run view, every chart is drawn in the run's color (the same
//     color the UI uses for the run elsewhere). Shapes/opacity distinguish
//     within-run step or category variation.
//   - In comparison, each run's data is drawn in that run's color; lines,
//     bars, histograms, and scatters stack/overlay across runs.
//
// Filter chips above each chart let the user toggle:
//   - tag chips (the tags the user attached via `tags=`)
//   - step chips (each emission's step number)
//   - run chips (only in comparison)
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '@/store'
import { Modal } from '@/components/ui/modal'
import { HeaderActions } from '@/components/node-tabs/HeaderActions'
import { downloadCanvasPng } from '@/components/node-tabs/downloadHelpers'
import { buildEmbeddedUrl } from '@/hooks/useEmbeddedView'
import type { MetricEntry, LoggableMetricSeries } from '@/lib/api'
import { DEFAULT_RUN_COLOR, RUN_COLOR_PALETTE } from '@/lib/colors'
import { LineMetric } from '@/components/charts/LineMetric'
import { ComparisonLine } from '@/components/charts/comparison/ComparisonLine'
import { ComparisonBar } from '@/components/charts/comparison/ComparisonBar'
import { ComparisonScatter } from '@/components/charts/comparison/ComparisonScatter'
import { ComparisonHistogram } from '@/components/charts/comparison/ComparisonHistogram'
import type { SeriesFor } from '@/components/charts/seriesFor'
import { BarMetric } from '@/components/charts/BarMetric'
import { PieMetric } from '@/components/charts/PieMetric'
import { ScatterMetric } from '@/components/charts/ScatterMetric'
import { HistogramMetric } from '@/components/charts/HistogramMetric'
import { ShapeIcon } from '@/components/charts/ShapeIcon'
import { ComparisonGrid } from '@/components/shared/ComparisonGrid'
import { getGridDimensions } from '@/lib/grid'
import { cn } from '@/lib/utils'

// Per-row vertical allocation for the comparison-pie split-panel grid.
// Without scaling by row count, 4+ runs (which need ≥2 rows) would
// squeeze each pie into ~100px and the chart effectively disappears.
const PIE_ROW_PX = 220

// Chart types whose renderers support zoom/pan and therefore expose a
// Reset zoom affordance. Bar/pie are snapshots with no zoom state.
const ZOOMABLE_CHART_TYPES = new Set(['line', 'scatter', 'histogram'])
import {
  UNTAGGED_KEY,
  entriesMatchingTags,
  scatterLabels,
  shapeForLabel,
} from '@/components/charts/scatterShape'
import { useTagChips } from '@/components/charts/useTagChips'

interface NodeMetricsProps {
  runId: string
  loggableId: string
  comparisonRunIds?: string[]
  // When true, fill the parent's height and scroll internally rather
  // than letting the metric stack expand naturally. Used inside
  // fixed-height DAG nodes.
  fillParent?: boolean
}

export function NodeMetrics({ runId, loggableId, comparisonRunIds, fillParent }: NodeMetricsProps) {
  if (comparisonRunIds) {
    return <ComparisonMetrics loggableId={loggableId} comparisonRunIds={comparisonRunIds} fillParent={fillParent} />
  }
  return <SingleRunMetrics runId={runId} loggableId={loggableId} fillParent={fillParent} />
}

function SingleRunMetrics({ runId, loggableId, fillParent }: { runId: string; loggableId: string; fillParent?: boolean }) {
  const metrics = useStore(s => s.runs.get(runId)?.loggableMetrics[loggableId]) ?? {}
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  const exportLimit = useStore(s => s.exportEntryLimit)
  const getOrAssignRunColor = useStore(s => s.getOrAssignRunColor)

  // Make sure this run has an assigned color — the store lazily assigns on demand.
  useEffect(() => {
    getOrAssignRunColor(runId)
  }, [runId, getOrAssignRunColor])

  const metricEntries = useMemo(() => {
    const all = Object.entries(metrics)
    return exportLimit ? all.slice(0, exportLimit) : all
  }, [metrics, exportLimit])

  if (metricEntries.length === 0) {
    return <p className="text-xs text-muted-foreground">No metrics for this loggable</p>
  }

  if (fillParent) {
    return (
      <div className="h-full overflow-auto">
        <div className="space-y-4">
          {metricEntries.map(([name, series]) => (
            <MetricBlock key={name} name={name} series={series} color={runColor} runId={runId} loggableId={loggableId} />
          ))}
        </div>
      </div>
    )
  }
  return (
    <div className="space-y-4">
      {metricEntries.map(([name, series]) => (
        <MetricBlock key={name} name={name} series={series} color={runColor} runId={runId} loggableId={loggableId} />
      ))}
    </div>
  )
}

export function MetricBlock({
  name,
  series,
  color,
  fill,
  runId,
  loggableId,
}: {
  name: string
  series: LoggableMetricSeries
  color: string
  // When true, the block claims its parent's height so the chart can
  // grow to fill remaining space (grid-view card mode). The header,
  // tag chips, and label chips stay at their natural size; the chart
  // wrapper takes flex-1.
  fill?: boolean
  // Optional ids used to build the "Copy iframe URL" target. When
  // omitted (older callers or contexts where embedding makes no sense)
  // the iframe row is hidden from the download popover.
  runId?: string
  loggableId?: string
}) {
  // Tags only apply to line metrics — every other type is a snapshot
  // and the v3 SDK strips tags off non-line emissions before they go on
  // the wire. Skip the chip computation when we know the metric isn't
  // line-typed so the empty chip row doesn't render.
  const allTags = useMemo(() => {
    if (series.type !== 'line') return [] as string[]
    const tags = new Set<string>()
    let hasUntagged = false
    for (const e of series.entries) {
      if (e.tags.length === 0) hasUntagged = true
      for (const t of e.tags) tags.add(t)
    }
    const out = [...tags].sort()
    // Only surface the (untagged) chip when tags ARE used elsewhere on this
    // metric — otherwise every entry is untagged and the chip is noise.
    if (hasUntagged && tags.size > 0) out.unshift(UNTAGGED_KEY)
    return out
  }, [series.type, series.entries])

  const { active: activeTags, toggle } = useTagChips(allTags)

  // Label chips drive scatter (sub-series toggling) and histogram
  // (overlapping distributions). Both store value as `{label: ...}`,
  // so the same `scatterLabels` helper extracts both.
  const allLabels = useMemo(
    () =>
      series.type === 'scatter' || series.type === 'histogram'
        ? scatterLabels(series.entries)
        : [],
    [series.type, series.entries],
  )
  const { active: activeLabels, toggle: toggleLabel } = useTagChips(allLabels)

  const tagFiltered = useMemo(
    // When no tag chips exist (e.g. nothing was ever tagged on this metric),
    // skip filtering — otherwise every entry, having neither tags nor an
    // active (untagged) chip to satisfy, gets excluded.
    //
    // For line metrics we keep all entries: deselected tags render as
    // soft-gray datasets inside LineMetric instead of being filtered.
    () => {
      if (allTags.length === 0) return series.entries
      if (series.type === 'line') return series.entries
      return entriesMatchingTags(series.entries, activeTags)
    },
    [series.entries, series.type, activeTags, allTags],
  )

  const inactiveTags = useMemo(() => {
    if (series.type !== 'line' || allTags.length === 0) return undefined
    return new Set(allTags.filter(t => !activeTags.has(t)))
  }, [series.type, allTags, activeTags])

  // Drop scatter labels the user has toggled off. Histogram has its own
  // path that filters the labeled value dict at render time (so the
  // shared min/max recalculates without us mutating the entry here).
  const filtered = useMemo(() => {
    if (series.type !== 'scatter' || allLabels.length === 0) return tagFiltered
    return tagFiltered
      .map(e => {
        const v = e.value as Record<string, unknown> | undefined
        if (!v || typeof v !== 'object') return e
        const kept: Record<string, unknown> = {}
        for (const k of Object.keys(v)) if (activeLabels.has(k)) kept[k] = v[k]
        return { ...e, value: kept }
      })
      // Hide entries whose labels are all toggled off, so we don't render
      // an empty chart slot for them.
      .filter(e => Object.keys(e.value as Record<string, unknown>).length > 0)
  }, [series.type, tagFiltered, activeLabels, allLabels.length])

  // Whether label chips should preview the rendered color (when the
  // emission was made with `colors=True`) or stay color-neutral with
  // a shape preview (the default). Read from the latest entry — for
  // scatter and histogram, that's the only entry once overwrites land.
  const latestColorsFlag = series.entries.length > 0
    ? series.entries[series.entries.length - 1].colors === true
    : false

  // Reset-zoom plumbing for line / scatter / histogram charts. The
  // chart components watch `resetSignal` and call chart.resetZoom()
  // when it changes, so the button can live up here next to the tag
  // chips instead of floating over the canvas.
  const [resetSignal, setResetSignal] = useState(0)
  const handleResetZoom = useCallback(() => setResetSignal(c => c + 1), [])
  const showResetButton = ZOOMABLE_CHART_TYPES.has(series.type)

  const [modalOpen, setModalOpen] = useState(false)
  const chartWrapperRef = useRef<HTMLDivElement>(null)
  const iframeUrl = runId && loggableId
    ? buildEmbeddedUrl({ runId, node: loggableId, metric: name })
    : undefined

  return (
    <div data-export-atom="chart" className={fill ? 'h-full flex flex-col min-h-0' : undefined}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground truncate flex-1 min-w-0">{name}</span>
        <HeaderActions
          onExpand={() => setModalOpen(true)}
          onDownloadPng={() => downloadCanvasPng(chartWrapperRef.current, name)}
          iframeUrl={iframeUrl}
          onResetZoom={showResetButton ? handleResetZoom : undefined}
        />
      </div>
      {allTags.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {allTags.map(t => (
            <Chip
              key={`tag:${t}`}
              label={t === UNTAGGED_KEY ? '(untagged)' : t}
              active={activeTags.has(t)}
              onClick={() => toggle(t)}
            />
          ))}
        </div>
      )}
      {allLabels.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {allLabels.map((l, i) => {
            const chipColor = latestColorsFlag
              ? RUN_COLOR_PALETTE[i % RUN_COLOR_PALETTE.length]
              : color
            // Scatter varies points by shape, so the chip previews the
            // matching glyph. Histogram only varies by color (areas
            // overlap as filled regions, not glyphs), so its chip is
            // always a colored dot. When `colors=True`, the scatter
            // chart already encodes the label distinction via color, so
            // shape rotation collapses to circle on both the chip and
            // the chart.
            const chipShape = series.type === 'scatter' && !latestColorsFlag
              ? shapeForLabel(l, allLabels)
              : 'circle'
            return (
              <Chip
                key={`label:${l}`}
                label={l}
                active={activeLabels.has(l)}
                onClick={() => toggleLabel(l)}
                icon={<ShapeIcon shape={chipShape} color={chipColor} />}
              />
            )
          })}
        </div>
      )}
      <div ref={chartWrapperRef} className={fill ? 'mt-1 flex-1 min-h-0' : 'mt-1'}>
        <SingleRunChart
          type={series.type}
          entries={filtered}
          color={color}
          allLabels={allLabels}
          activeLabels={series.type === 'histogram' ? activeLabels : undefined}
          inactiveTags={inactiveTags}
          resetSignal={resetSignal}
          fill={fill}
        />
      </div>
      {modalOpen && (
        <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={name} widthClass="max-w-6xl">
          <div className="h-[60vh] flex flex-col min-h-0">
            <SingleRunChart
              type={series.type}
              entries={filtered}
              color={color}
              allLabels={allLabels}
              activeLabels={series.type === 'histogram' ? activeLabels : undefined}
              inactiveTags={inactiveTags}
              resetSignal={resetSignal}
              fill
            />
          </div>
        </Modal>
      )}
    </div>
  )
}

function Chip({
  label,
  active,
  onClick,
  icon,
}: {
  label: string
  active: boolean
  onClick: () => void
  icon?: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={
        'text-[10px] rounded px-1.5 py-0.5 border flex items-center gap-1 ' +
        (active
          ? 'bg-accent text-accent-foreground border-accent'
          : 'bg-muted/40 text-muted-foreground border-border hover:bg-muted')
      }
    >
      {icon}
      {label}
    </button>
  )
}

// Exported for the mobile feed, which renders charts without the
// desktop MetricBlock chrome (header actions, chip rows).
export function SingleRunChart({
  type,
  entries,
  color,
  allLabels,
  activeLabels,
  inactiveTags,
  resetSignal,
  fill,
}: {
  type: string
  entries: MetricEntry[]
  color: string
  allLabels: string[]
  // Forwarded to HistogramMetric so it can re-bin against only the
  // user-selected labels' samples (the others are folded out of the
  // shared min/max as well as the per-label Areas).
  activeLabels?: Set<string>
  // Forwarded to LineMetric so deselected tag chips dim the muted
  // segments to soft gray instead of being filtered out of the data.
  inactiveTags?: Set<string>
  // Counter forwarded to LineMetric / ScatterMetric so the chip-row
  // "Reset zoom" button can trigger chart.resetZoom() without floating
  // over the canvas.
  resetSignal?: number
  fill?: boolean
}) {
  if (type === 'line') return <LineMetric entries={entries} color={color} fill={fill} inactiveTags={inactiveTags} resetSignal={resetSignal} />
  if (type === 'histogram')
    return (
      <HistogramMetric
        entries={entries}
        color={color}
        fill={fill}
        activeLabels={activeLabels}
        allLabels={allLabels}
        resetSignal={resetSignal}
      />
    )
  if (type === 'scatter') return <ScatterMetric entries={entries} color={color} allLabels={allLabels} fill={fill} resetSignal={resetSignal} />
  // Bar/pie are now snapshots: only ever one entry. Render that single
  // chart full-bleed inside the card body — no step label, no stack.
  const latest = entries.length > 0 ? entries[entries.length - 1] : null
  if (!latest) return null
  if (fill) {
    return (
      <div className="h-full min-h-0">
        {type === 'bar' && <BarMetric entry={latest} color={color} fill />}
        {type === 'pie' && <PieMetric entry={latest} fill />}
      </div>
    )
  }
  return (
    <div>
      {type === 'bar' && <BarMetric entry={latest} color={color} />}
      {type === 'pie' && <PieMetric entry={latest} />}
    </div>
  )
}

// ─── Comparison ────────────────────────────────────────────────────────────

function ComparisonMetrics({
  loggableId,
  comparisonRunIds,
  fillParent,
}: {
  loggableId: string
  comparisonRunIds: string[]
  fillParent?: boolean
}) {
  const runs = useStore(s => s.runs)
  const runColors = useStore(s => s.runColors)
  const runNames = useStore(s => s.runNames)
  const getOrAssignRunColor = useStore(s => s.getOrAssignRunColor)

  useEffect(() => {
    for (const rid of comparisonRunIds) getOrAssignRunColor(rid)
  }, [comparisonRunIds, getOrAssignRunColor])

  // All run chips start selected. Only *newly appearing* runs get auto-added
  // to activeRuns; reaffirming the current set from a WS update must not
  // resurrect a run the user deselected.
  const [activeRuns, setActiveRuns] = useState<Set<string>>(() => new Set(comparisonRunIds))
  const seenRuns = useRef<Set<string>>(new Set(comparisonRunIds))
  useEffect(() => {
    const fresh: string[] = []
    for (const rid of comparisonRunIds) {
      if (!seenRuns.current.has(rid)) {
        seenRuns.current.add(rid)
        fresh.push(rid)
      }
    }
    if (fresh.length === 0) return
    setActiveRuns(prev => {
      const next = new Set(prev)
      for (const rid of fresh) next.add(rid)
      return next
    })
  }, [comparisonRunIds])

  // Prefer the user's client-side custom name → the SDK-emitted
  // `run_name` (from `nb.init(name=...)`) → the script filename as a
  // last resort. The chips in comparison views used to skip the
  // `run_name` step and always fell back to the script's filename,
  // which made user-supplied run names invisible.
  const runNameFor = (rid: string) =>
    runNames.get(rid) ||
    runs.get(rid)?.summary.run_name ||
    runs.get(rid)?.summary.script_path.split('/').pop() ||
    rid

  // Collect every metric name across runs, keyed by its type (use the first
  // type we see — type is locked on the SDK side anyway).
  const metricNames = useMemo(() => {
    const byName = new Map<string, string>()
    for (const rid of comparisonRunIds) {
      const runMetrics = runs.get(rid)?.loggableMetrics[loggableId]
      if (!runMetrics) continue
      for (const [name, series] of Object.entries(runMetrics)) {
        if (!byName.has(name)) byName.set(name, series.type)
      }
    }
    return byName
  }, [comparisonRunIds, runs, loggableId])

  if (metricNames.size === 0) {
    return <p className="text-xs text-muted-foreground">No metrics for this loggable</p>
  }

  const effectiveRunIds = comparisonRunIds.filter(rid => activeRuns.has(rid))

  return (
    <div className={cn('space-y-4', fillParent && 'h-full overflow-auto')}>
      {[...metricNames.entries()].map(([name, type]) => (
        <ComparisonMetricBlock
          key={name}
          name={name}
          type={type}
          loggableId={loggableId}
          runIds={effectiveRunIds}
          comparisonRunIds={comparisonRunIds}
          activeRuns={activeRuns}
          setActiveRuns={setActiveRuns}
          runColors={runColors}
          runNameFor={runNameFor}
        />
      ))}
    </div>
  )
}

function ComparisonMetricBlock({
  name,
  type,
  loggableId,
  runIds,
  comparisonRunIds,
  activeRuns,
  setActiveRuns,
  runColors,
  runNameFor,
}: {
  name: string
  type: string
  loggableId: string
  runIds: string[]
  comparisonRunIds: string[]
  activeRuns: Set<string>
  setActiveRuns: (updater: (prev: Set<string>) => Set<string>) => void
  runColors: Map<string, string>
  runNameFor: (rid: string) => string
}) {
  const runs = useStore(s => s.runs)

  const allTags = useMemo(() => {
    const tags = new Set<string>()
    let hasUntagged = false
    for (const rid of comparisonRunIds) {
      const series = runs.get(rid)?.loggableMetrics[loggableId]?.[name]
      if (!series) continue
      for (const e of series.entries) {
        if (e.tags.length === 0) hasUntagged = true
        for (const t of e.tags) tags.add(t)
      }
    }
    const out = [...tags].sort()
    if (hasUntagged && tags.size > 0) out.unshift(UNTAGGED_KEY)
    return out
  }, [comparisonRunIds, runs, loggableId, name])

  const { active: activeTags, toggle: toggleTag } = useTagChips(allTags)

  // For scatter, also collect the union of dict-key labels across runs so
  // the user can hide whole sub-series the way bar/pie users hide categories.
  const allLabels = useMemo(() => {
    if (type !== 'scatter') return [] as string[]
    const labels = new Set<string>()
    for (const rid of comparisonRunIds) {
      const series = runs.get(rid)?.loggableMetrics[loggableId]?.[name]
      if (!series) continue
      for (const e of series.entries) {
        const v = e.value
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          for (const k of Object.keys(v as Record<string, unknown>)) labels.add(k)
        }
      }
    }
    return [...labels].sort()
  }, [type, comparisonRunIds, runs, loggableId, name])

  const { active: activeLabels, toggle: toggleLabel } = useTagChips(allLabels)

  const [modalOpen, setModalOpen] = useState(false)
  const chartWrapperRef = useRef<HTMLDivElement>(null)
  const [resetSignal, setResetSignal] = useState(0)
  const handleResetZoom = useCallback(() => setResetSignal((c) => c + 1), [])
  const showResetButton = ZOOMABLE_CHART_TYPES.has(type)

  return (
    <div data-export-atom="chart">
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-medium text-foreground truncate flex-1 min-w-0">{name}</span>
        <HeaderActions
          onExpand={() => setModalOpen(true)}
          onDownloadPng={() => downloadCanvasPng(chartWrapperRef.current, name)}
          onResetZoom={showResetButton ? handleResetZoom : undefined}
        />
      </div>
      <div className="mt-1 flex flex-wrap gap-1">
        {comparisonRunIds.map(rid => {
          const active = activeRuns.has(rid)
          const color = runColors.get(rid) ?? DEFAULT_RUN_COLOR
          return (
            <button
              key={rid}
              onClick={() =>
                setActiveRuns(prev => {
                  const next = new Set(prev)
                  if (next.has(rid)) next.delete(rid)
                  else next.add(rid)
                  return next
                })
              }
              className={
                'text-[10px] rounded px-1.5 py-0.5 border flex items-center gap-1 ' +
                (active
                  ? 'bg-accent text-accent-foreground border-accent'
                  : 'bg-muted/40 text-muted-foreground border-border hover:bg-muted')
              }
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: color }}
              />
              {runNameFor(rid)}
            </button>
          )
        })}
        {allTags.map(t => (
          <Chip
            key={`tag:${t}`}
            label={t === UNTAGGED_KEY ? '(untagged)' : t}
            active={activeTags.has(t)}
            onClick={() => toggleTag(t)}
          />
        ))}
      </div>
      {allLabels.length > 0 && (
        <div className="mt-1 flex flex-wrap gap-1">
          {allLabels.map(l => (
            <Chip
              key={`label:${l}`}
              label={l}
              active={activeLabels.has(l)}
              onClick={() => toggleLabel(l)}
              // Color-neutral on the chip; the point's color is the run's.
              icon={<ShapeIcon shape={shapeForLabel(l, allLabels)} color="var(--color-popover-foreground)" />}
            />
          ))}
        </div>
      )}
      <div ref={chartWrapperRef} className="mt-1">
        <ComparisonChart
          type={type}
          name={name}
          loggableId={loggableId}
          runIds={runIds}
          activeTags={activeTags}
          allTags={allTags}
          activeLabels={activeLabels}
          allLabels={allLabels}
          resetSignal={resetSignal}
        />
      </div>
      {modalOpen && (
        <Modal open={modalOpen} onClose={() => setModalOpen(false)} title={name} widthClass="max-w-6xl">
          <div className="h-[60vh] flex flex-col min-h-0">
            <ComparisonChart
              type={type}
              name={name}
              loggableId={loggableId}
              runIds={runIds}
              activeTags={activeTags}
              allTags={allTags}
              activeLabels={activeLabels}
              allLabels={allLabels}
              resetSignal={resetSignal}
            />
          </div>
        </Modal>
      )}
    </div>
  )
}

function ComparisonChart({
  type,
  name,
  loggableId,
  runIds,
  activeTags,
  allTags,
  activeLabels,
  allLabels,
  resetSignal,
}: {
  type: string
  name: string
  loggableId: string
  runIds: string[]
  activeTags: Set<string>
  allTags: string[]
  activeLabels: Set<string>
  allLabels: string[]
  resetSignal?: number
}) {
  const runs = useStore(s => s.runs)
  const runColors = useStore(s => s.runColors)
  const runNames = useStore(s => s.runNames)

  // Prefer the user's client-side custom name → the SDK-emitted
  // `run_name` (from `nb.init(name=...)`) → the script filename as a
  // last resort. The chips in comparison views used to skip the
  // `run_name` step and always fell back to the script's filename,
  // which made user-supplied run names invisible.
  const runNameFor = (rid: string) =>
    runNames.get(rid) ||
    runs.get(rid)?.summary.run_name ||
    runs.get(rid)?.summary.script_path.split('/').pop() ||
    rid
  const seriesFor = useCallback(
    (rid: string) => {
      const s = runs.get(rid)?.loggableMetrics[loggableId]?.[name]
      if (!s) return undefined
      // Skip the tag filter when no tags exist on this metric across any
      // run (otherwise every untagged entry — which is all of them for
      // bar/pie/histogram and tag-less scatter — gets dropped because
      // activeTags is empty and `entriesMatchingTags` only keeps `[]`-
      // tagged entries when activeTags contains UNTAGGED_KEY). Line
      // metrics also bypass this filter — they use the mute-via-color
      // path on a single combined dataset instead.
      const skipTagFilter = allTags.length === 0
      const tagFiltered = skipTagFilter
        ? s.entries
        : entriesMatchingTags(s.entries, activeTags)
      // Drop deselected scatter labels here so the comparison renderer
      // only sees the slots the user wants. Pie and bar values are
      // also dicts but we leave them untouched — their toggling story
      // lives in their own legend/category rendering, not this chip row.
      if (s.type === 'scatter' && allLabels.length > 0) {
        const trimmed = tagFiltered
          .map(e => {
            const v = e.value as Record<string, unknown> | undefined
            if (!v || typeof v !== 'object') return e
            const kept: Record<string, unknown> = {}
            for (const k of Object.keys(v)) if (activeLabels.has(k)) kept[k] = v[k]
            return { ...e, value: kept }
          })
          .filter(e => Object.keys(e.value as Record<string, unknown>).length > 0)
        return { ...s, entries: trimmed }
      }
      return { ...s, entries: tagFiltered }
    },
    [runs, loggableId, name, activeTags, allTags.length, activeLabels, allLabels.length],
  )

  if (type === 'line') return <ComparisonLine runIds={runIds} runColors={runColors} runNameFor={runNameFor} seriesFor={seriesFor} resetSignal={resetSignal} />
  if (type === 'bar') return <ComparisonBar runIds={runIds} runColors={runColors} runNameFor={runNameFor} seriesFor={seriesFor} />
  if (type === 'histogram') return <ComparisonHistogram runIds={runIds} runColors={runColors} runNameFor={runNameFor} seriesFor={seriesFor} resetSignal={resetSignal} />
  if (type === 'scatter') return <ComparisonScatter runIds={runIds} runColors={runColors} runNameFor={runNameFor} seriesFor={seriesFor} resetSignal={resetSignal} />
  // Pie: one pie per run, rendered in the standard split-panel layout
  // (matches Logs/Images/Audio comparison styling). The cell header
  // already shows the run name + color stripe, so we just render the
  // chart in the body. The outer height scales with the grid's row
  // count so each pie panel gets a consistent ~PIE_ROW_PX of vertical
  // space (see PIE_ROW_PX comment at module scope).
  const { rows: pieRows } = getGridDimensions(runIds.length)
  return (
    <div style={{ height: Math.max(1, pieRows) * PIE_ROW_PX }}>
      <ComparisonGrid runIds={runIds} fillParent>
        {(rid) => {
          const s = seriesFor(rid)
          const latest = s?.entries[s.entries.length - 1]
          if (!latest) {
            return <p className="text-[10px] text-muted-foreground p-2">No pie data</p>
          }
          return (
            <div className="h-full p-2">
              <PieMetric entry={latest} fill />
            </div>
          )
        }}
      </ComparisonGrid>
    </div>
  )
}



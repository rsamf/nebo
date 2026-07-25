import { useCallback, useMemo, useState } from 'react'
import { useStore } from '@/store'
import type { AudioEntry, ImageEntry } from '@/store'
import { api, type LogEntry, type LoggableMetricSeries } from '@/lib/api'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { SingleRunChart } from '@/components/node-tabs/NodeMetrics'
import { scatterLabels } from '@/components/charts/scatterShape'
import { ImageWithLabels } from '@/components/shared/ImageWithLabels'
import { Modal } from '@/components/ui/modal'
import { LongPressChartGate } from './LongPressChartGate'
import { MetricPreview } from './MetricPreview'
import { Chip, LEVEL_FILTERS, Segmented, type LevelFilter } from './primitives'
import { latestMetricLabel, loggableDisplayName } from './util'
import { cn, formatTimestamp, mediaEntryKey } from '@/lib/utils'

// Flat feed of everything the run logs: a pipeline-stage chip rail and a
// type filter on top, then one card per metric / media stream / log tail.
// Tapping a metric card expands the full chart inline (with the tracker
// playhead); charts stay mounted per the useChartJs remount invariant.

type TypeFilter = 'all' | 'metrics' | 'media' | 'logs'

const TYPE_FILTERS: { value: TypeFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'metrics', label: 'Metrics' },
  { value: 'media', label: 'Media' },
  { value: 'logs', label: 'Logs' },
]

export function MobileFeed({ runId }: { runId: string }) {
  const run = useStore(s => s.runs.get(runId))
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  const [stage, setStage] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')

  const globalId = run?.globalLoggable?.loggableId ?? '__global__'
  const agentId = run?.agentLoggable?.loggableId ?? '__agent__'

  // One pass over the (potentially 10k+) log array per change, instead
  // of each stage row re-filtering the whole thing.
  const logsByStage = useMemo(() => {
    const out = new Map<string, LogEntry[]>()
    for (const l of run?.logs ?? []) {
      const id = l.node ?? globalId
      const arr = out.get(id)
      if (arr) arr.push(l)
      else out.set(id, [l])
    }
    return out
  }, [run?.logs, globalId])

  // Stage rail: every DAG node (registration order), then global/agent
  // when they have content. O(nodes) with map lookups — cheap enough to
  // run per render.
  const stages: string[] = Object.keys(run?.graph?.nodes ?? {})
  const hasContent = (id: string) =>
    Object.keys(run?.loggableMetrics[id] ?? {}).length > 0 ||
    (run?.loggableImages[id]?.length ?? 0) > 0 ||
    (run?.loggableAudio[id]?.length ?? 0) > 0 ||
    logsByStage.has(id)
  for (const extra of [globalId, agentId]) {
    if (!stages.includes(extra) && hasContent(extra)) stages.push(extra)
  }

  if (!run) return null

  const visibleStages = stage ? stages.filter(s => s === stage) : stages

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* No horizontal padding on this wrapper — the chip rail must clip
          at the device edge and scroll under it; the inset lives inside
          the scroller (rail px-4) and on the non-scrolling row (mx-4). */}
      <div className="shrink-0 border-b border-border">
        <div className="no-scrollbar flex gap-2 overflow-x-auto px-4 pb-2.5 pt-3">
          <Chip label="All" active={stage === null} onTap={() => setStage(null)} />
          {stages.map(id => (
            <Chip
              key={id}
              label={loggableDisplayName(run, id)}
              active={stage === id}
              onTap={() => setStage(prev => (prev === id ? null : id))}
            />
          ))}
        </div>
        <Segmented
          className="mx-4 mb-2.5"
          options={TYPE_FILTERS}
          value={typeFilter}
          onChange={setTypeFilter}
        />
      </div>

      <div className="flex flex-1 flex-col gap-2.5 overflow-y-auto px-4 pb-5 pt-3">
        {visibleStages.map(id => (
          <StageRows
            key={id}
            runId={runId}
            loggableId={id}
            nodeLabel={loggableDisplayName(run, id)}
            logs={logsByStage.get(id)}
            typeFilter={typeFilter}
            color={runColor}
          />
        ))}
        {visibleStages.length === 0 && (
          <div className="py-10 text-center text-sm text-muted-foreground">
            Nothing logged yet
          </div>
        )}
      </div>
    </div>
  )
}

function StageRows({
  runId,
  loggableId,
  nodeLabel,
  logs,
  typeFilter,
  color,
}: {
  runId: string
  loggableId: string
  nodeLabel: string
  logs: LogEntry[] | undefined
  typeFilter: TypeFilter
  color: string
}) {
  const run = useStore(s => s.runs.get(runId))
  const metrics = run?.loggableMetrics[loggableId] ?? {}
  const images = run?.loggableImages[loggableId]
  const audio = run?.loggableAudio[loggableId]

  const imagesByName = useMemo(() => groupByName(images ?? []), [images])
  const audioByName = useMemo(() => groupByName(audio ?? []), [audio])

  return (
    <>
      {(typeFilter === 'all' || typeFilter === 'metrics') &&
        Object.entries(metrics).map(([name, series]) => (
          <MetricFeedCard
            key={`m:${loggableId}:${name}`}
            name={name}
            series={series}
            nodeLabel={nodeLabel}
            color={color}
          />
        ))}
      {(typeFilter === 'all' || typeFilter === 'media') && (
        <>
          {[...imagesByName.entries()].map(([name, entries]) => (
            <ImageFeedCard
              key={`i:${loggableId}:${name}`}
              runId={runId}
              loggableId={loggableId}
              name={name}
              entries={entries}
              nodeLabel={nodeLabel}
            />
          ))}
          {[...audioByName.entries()].map(([name, entries]) => (
            <AudioFeedCard
              key={`a:${loggableId}:${name}`}
              runId={runId}
              name={name}
              entries={entries}
              nodeLabel={nodeLabel}
            />
          ))}
        </>
      )}
      {(typeFilter === 'all' || typeFilter === 'logs') && logs && logs.length > 0 && (
        <LogsFeedCard key={`l:${loggableId}`} logs={logs} nodeLabel={nodeLabel} />
      )}
    </>
  )
}

function groupByName<T extends { name: string }>(entries: T[]): Map<string, T[]> {
  const out = new Map<string, T[]>()
  for (const e of entries) {
    const k = e.name || 'media'
    const arr = out.get(k)
    if (arr) arr.push(e)
    else out.set(k, [e])
  }
  return out
}

function MetricFeedCard({
  name,
  series,
  nodeLabel,
  color,
}: {
  name: string
  series: LoggableMetricSeries
  nodeLabel: string
  color: string
}) {
  const [expanded, setExpanded] = useState(false)
  const lineSmoothing = useStore(s => s.settings.lineSmoothing ?? 0)
  const selectTimelineStep = useStore(s => s.selectTimelineStep)

  const allLabels = useMemo(
    () =>
      series.type === 'scatter' || series.type === 'histogram'
        ? scatterLabels(series.entries)
        : [],
    [series.type, series.entries],
  )

  // While a long-pressed line chart is focused, the step filter follows
  // the finger — the touch replacement for LineMetric's built-in click
  // handler, which the gate's pointer-events:none canvas never receives.
  // Reads the live timeline to skip no-op sets: this fires per animation
  // frame during a scrub.
  const scrubStep = useCallback(
    (x: number) => {
      const rounded = Math.round(x)
      const t = useStore.getState().timeline
      if (t.mode !== 'step' || t.step !== rounded) selectTimelineStep(rounded)
    },
    [selectTimelineStep],
  )

  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <button onClick={() => setExpanded(e => !e)} className="flex min-h-[30px] w-full items-center gap-3 text-left">
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium">{name}</div>
          <div className="text-[11px] text-muted-foreground">{nodeLabel}</div>
        </div>
        {series.type === 'line' && (
          <div className="shrink-0 text-[15px] font-semibold tabular-nums">{latestMetricLabel(series)}</div>
        )}
        {!expanded && (
          <MetricPreview series={series} color={color} smoothing={lineSmoothing} className="shrink-0" />
        )}
      </button>
      {/* Chart canvases must stay mounted while visible (useChartJs mount
          effect has [] deps) — the whole block toggles, never the canvas
          within it, so collapse/expand fully remounts the chart. */}
      {expanded && (
        <LongPressChartGate
          className="mt-2.5 h-48"
          onScrubX={series.type === 'line' ? scrubStep : undefined}
        >
          <SingleRunChart
            type={series.type}
            entries={series.entries}
            color={color}
            allLabels={allLabels}
            fill
          />
        </LongPressChartGate>
      )}
    </div>
  )
}

function ImageFeedCard({
  runId,
  loggableId,
  name,
  entries,
  nodeLabel,
}: {
  runId: string
  loggableId: string
  name: string
  entries: ImageEntry[]
  nodeLabel: string
}) {
  const timelineFilter = useTimelineFilter()
  const [lightbox, setLightbox] = useState<ImageEntry | null>(null)
  const visible = useMemo(() => {
    const filtered = timelineFilter ? entries.filter(e => timelineFilter.matchEntry(e)) : entries
    return filtered.slice(-24)
  }, [entries, timelineFilter])

  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <div className="mb-2.5 flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{name}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{nodeLabel}</span>
      </div>
      {visible.length === 0 ? (
        <div className="text-[11px] text-muted-foreground">No images at this step</div>
      ) : (
        <div className="no-scrollbar flex gap-2 overflow-x-auto">
          {visible.map((e, i) => (
            <button
              key={mediaEntryKey(e, i)}
              onClick={() => setLightbox(e)}
              className="relative h-24 w-24 shrink-0 overflow-hidden rounded-lg border border-border"
            >
              <img
                src={api.mediaUrl(runId, e.mediaId)}
                alt={e.name}
                loading="lazy"
                className="h-full w-full object-cover"
              />
              {e.step != null && (
                <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1 font-mono text-[9px] text-white">
                  s{e.step}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
      {lightbox && (
        <Modal
          open
          onClose={() => setLightbox(null)}
          title={`${name}${lightbox.step != null ? ` · step ${lightbox.step}` : ''}`}
        >
          <div className="p-3">
            <ImageWithLabels
              src={api.mediaUrl(runId, lightbox.mediaId)}
              labels={lightbox.labels}
              loggableName={loggableId}
              imageName={lightbox.name ?? ''}
              alt={lightbox.name}
            />
          </div>
        </Modal>
      )}
    </div>
  )
}

function AudioFeedCard({
  runId,
  name,
  entries,
  nodeLabel,
}: {
  runId: string
  name: string
  entries: AudioEntry[]
  nodeLabel: string
}) {
  const timelineFilter = useTimelineFilter()
  const visible = useMemo(() => {
    const filtered = timelineFilter ? entries.filter(e => timelineFilter.matchEntry(e)) : entries
    return filtered.slice(-10)
  }, [entries, timelineFilter])

  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <div className="mb-2.5 flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{name}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{nodeLabel}</span>
      </div>
      {visible.length === 0 ? (
        <div className="text-[11px] text-muted-foreground">No audio at this step</div>
      ) : (
        <div className="flex flex-col gap-2">
          {visible.map((e, i) => (
            <div key={mediaEntryKey(e, i)} className="flex items-center gap-2">
              {e.step != null && (
                <span className="shrink-0 font-mono text-[10px] text-muted-foreground">s{e.step}</span>
              )}
              <audio controls preload="none" src={api.mediaUrl(runId, e.mediaId)} className="h-9 w-full" />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function LogsFeedCard({ logs, nodeLabel }: { logs: LogEntry[]; nodeLabel: string }) {
  const [level, setLevel] = useState<LevelFilter>('All')
  const timelineFilter = useTimelineFilter()

  const visible = useMemo(() => {
    let filtered = logs
    if (level !== 'All') {
      const want = level.toLowerCase()
      // "warn" chip matches the SDK's "warning" level string too.
      filtered = filtered.filter(l => l.level === want || (want === 'warn' && l.level === 'warning'))
    }
    if (timelineFilter) filtered = filtered.filter(e => timelineFilter.matchEntry(e))
    return filtered.slice(-100)
  }, [logs, level, timelineFilter])

  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <div className="mb-2 flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">Logs</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{nodeLabel} · tail</span>
      </div>
      <div className="mb-2 flex gap-1.5">
        {LEVEL_FILTERS.map(lvl => (
          <Chip key={lvl} label={lvl} active={level === lvl} onTap={() => setLevel(lvl)} />
        ))}
      </div>
      <div className="no-scrollbar max-h-40 overflow-y-auto font-mono">
        {visible.length === 0 && (
          <div className="py-1 text-[11px] text-muted-foreground">No matching log lines</div>
        )}
        {visible.map((l, i) => (
          <div
            key={i}
            className={cn(
              'rounded px-1.5 py-0.5 text-[11px] leading-[1.45]',
              (l.level === 'error') && 'bg-red-500/10 text-red-400',
              (l.level === 'warning' || l.level === 'warn') && 'text-yellow-500',
            )}
          >
            <span className="mr-1.5 text-muted-foreground">{formatTimestamp(l.timestamp)}</span>
            {l.message}
          </div>
        ))}
      </div>
    </div>
  )
}

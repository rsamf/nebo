import { useCallback, useEffect, useMemo, useState } from 'react'
import { motion } from 'motion/react'
import { useStore } from '@/store'
import type { AudioEntry, ImageEntry } from '@/store'
import { resolveNavModality, type NavModality } from '@/lib/navTarget'
import { api, type TextEntry, type LoggableMetricSeries } from '@/lib/api'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { SingleRunChart } from '@/components/node-tabs/NodeMetrics'
import { scatterLabels } from '@/components/charts/scatterShape'
import { ImageWithLabels } from '@/components/shared/ImageWithLabels'
import { ActionCard } from '@/components/actions/ActionCard'
import { Modal } from '@/components/ui/modal'
import { LongPressChartGate } from './LongPressChartGate'
import { MetricPreview } from './MetricPreview'
import { Chip, Segmented } from './primitives'
import { latestMetricLabel, loggableDisplayName } from './util'
import { formatTimestamp, mediaEntryKey } from '@/lib/utils'

// Flat feed of everything the run logs: a pipeline-stage chip rail and a
// type filter on top, then one card per metric / media / text stream.
// Tapping a metric card expands the full chart inline (with the tracker
// playhead); charts stay mounted per the useChartJs remount invariant.

type TypeFilter = 'all' | 'metrics' | 'media' | 'text' | 'actions'

const TYPE_FILTERS: { value: TypeFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'metrics', label: 'Metrics' },
  { value: 'media', label: 'Media' },
  { value: 'text', label: 'Text' },
  { value: 'actions', label: 'Actions' },
]

// Deep-link landing: the feed's type filter is mobile's equivalent of the
// desktop tab, and card keys are the scroll anchors.
const MODALITY_TO_TYPE: Record<NavModality, TypeFilter> = {
  text: 'text', metric: 'metrics', image: 'media', audio: 'media',
  action: 'actions',
}
// `a` already belongs to audio, so scenes take `act`.
const MODALITY_TO_PREFIX: Record<NavModality, string> = {
  text: 't', metric: 'm', image: 'i', audio: 'a', action: 'act',
}
const HIGHLIGHT_MS = 3000

/** Scroll anchor + deep-link flash for one feed card. */
function FeedCardAnchor({ cardId, highlighted, children }: {
  cardId: string
  highlighted: boolean
  children: React.ReactNode
}) {
  return (
    <motion.div
      id={`mcard-${cardId}`}
      className="rounded-xl"
      animate={highlighted
        ? { boxShadow: [
            '0 0 0 0px rgba(59,130,246,0)',
            '0 0 0 3px rgba(59,130,246,0.85)',
            '0 0 0 3px rgba(59,130,246,0.85)',
            '0 0 0 0px rgba(59,130,246,0)',
          ] }
        : { boxShadow: '0 0 0 0px rgba(59,130,246,0)' }}
      transition={highlighted
        ? { duration: HIGHLIGHT_MS / 1000, times: [0, 0.08, 0.75, 1], ease: 'easeOut' }
        : { duration: 0 }}
    >
      {children}
    </motion.div>
  )
}

export function MobileFeed({ runId }: { runId: string }) {
  const run = useStore(s => s.runs.get(runId))
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  const [stage, setStage] = useState<string | null>(null)
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [highlightedCardId, setHighlightedCardId] = useState<string | null>(null)
  const pendingNavTarget = useStore(s => s.pendingNavTarget)
  const setPendingNavTarget = useStore(s => s.setPendingNavTarget)

  const globalId = run?.globalLoggable?.loggableId ?? '__global__'
  const agentId = run?.agentLoggable?.loggableId ?? '__agent__'

  // One pass over the (potentially 10k+) text array per change, instead
  // of each stage row re-filtering the whole thing.
  const textsByStage = useMemo(() => {
    const out = new Map<string, TextEntry[]>()
    for (const t of run?.texts ?? []) {
      const id = t.node ?? globalId
      const arr = out.get(id)
      if (arr) arr.push(t)
      else out.set(id, [t])
    }
    return out
  }, [run?.texts, globalId])

  // Stage rail: every DAG node (registration order), then global/agent
  // when they have content. O(nodes) with map lookups — cheap enough to
  // run per render.
  const stages: string[] = Object.keys(run?.graph?.nodes ?? {})
  const hasContent = (id: string) =>
    Object.keys(run?.loggableMetrics[id] ?? {}).length > 0 ||
    (run?.loggableImages[id]?.length ?? 0) > 0 ||
    (run?.loggableAudio[id]?.length ?? 0) > 0 ||
    (run?.loggableActions[id]?.length ?? 0) > 0 ||
    textsByStage.has(id)
  for (const extra of [globalId, agentId]) {
    if (!stages.includes(extra) && hasContent(extra)) stages.push(extra)
  }

  // Deep-link landing, mirroring the desktop flat view: switch to the type
  // filter that owns the card, drop a stage chip that would hide it,
  // scroll to it, flash it. Runs after every render (no dep array) since
  // the target's data may still be hydrating; all state writes happen in
  // the rAF callback, never synchronously in the effect body.
  useEffect(() => {
    if (!pendingNavTarget || !run) return
    const raf = requestAnimationFrame(() => {
      const hit = resolveNavModality(run, pendingNavTarget.loggableId, pendingNavTarget.name)
      if (!hit) return // not ingested yet — retried on the next render
      if (stage && stage !== pendingNavTarget.loggableId) setStage(null)
      const wantType = MODALITY_TO_TYPE[hit.modality]
      if (typeFilter !== wantType) {
        setTypeFilter(wantType)
        return // card mounts on the next render; scroll then
      }
      const cardId = `${MODALITY_TO_PREFIX[hit.modality]}:${pendingNavTarget.loggableId}:${hit.name}`
      const el = document.getElementById(`mcard-${cardId}`)
      if (!el) return
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setHighlightedCardId(cardId)
      setPendingNavTarget(null)
    })
    return () => cancelAnimationFrame(raf)
  })

  useEffect(() => {
    if (!highlightedCardId) return
    const t = setTimeout(() => setHighlightedCardId(null), HIGHLIGHT_MS)
    return () => clearTimeout(t)
  }, [highlightedCardId])

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
            texts={textsByStage.get(id)}
            typeFilter={typeFilter}
            color={runColor}
            highlightedCardId={highlightedCardId}
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
  texts,
  typeFilter,
  color,
  highlightedCardId,
}: {
  runId: string
  loggableId: string
  nodeLabel: string
  texts: TextEntry[] | undefined
  typeFilter: TypeFilter
  color: string
  highlightedCardId: string | null
}) {
  const run = useStore(s => s.runs.get(runId))
  const metrics = run?.loggableMetrics[loggableId] ?? {}
  const images = run?.loggableImages[loggableId]
  const audio = run?.loggableAudio[loggableId]
  const actionFrames = run?.loggableActions[loggableId]

  const imagesByName = useMemo(() => groupByName(images ?? []), [images])
  const audioByName = useMemo(() => groupByName(audio ?? []), [audio])
  const textsByName = useMemo(() => groupByName(texts ?? []), [texts])
  // Scene frames repeat their name once per step; the feed wants the
  // distinct scenes, each rendered as one live viewport.
  const sceneNames = useMemo(() => {
    const out: string[] = []
    const seen = new Set<string>()
    for (const frame of actionFrames ?? []) {
      const name = frame.name || 'scene'
      if (seen.has(name)) continue
      seen.add(name)
      out.push(name)
    }
    return out
  }, [actionFrames])

  const anchor = (cardId: string, node: React.ReactNode) => (
    <FeedCardAnchor key={cardId} cardId={cardId} highlighted={highlightedCardId === cardId}>
      {node}
    </FeedCardAnchor>
  )

  return (
    <>
      {(typeFilter === 'all' || typeFilter === 'metrics') &&
        Object.entries(metrics).map(([name, series]) => anchor(
          `m:${loggableId}:${name}`,
          <MetricFeedCard name={name} series={series} nodeLabel={nodeLabel} color={color} />,
        ))}
      {(typeFilter === 'all' || typeFilter === 'media') && (
        <>
          {[...imagesByName.entries()].map(([name, entries]) => anchor(
            `i:${loggableId}:${name}`,
            <ImageFeedCard
              runId={runId}
              loggableId={loggableId}
              name={name}
              entries={entries}
              nodeLabel={nodeLabel}
            />,
          ))}
          {[...audioByName.entries()].map(([name, entries]) => anchor(
            `a:${loggableId}:${name}`,
            <AudioFeedCard runId={runId} name={name} entries={entries} nodeLabel={nodeLabel} />,
          ))}
        </>
      )}
      {(typeFilter === 'all' || typeFilter === 'actions') &&
        sceneNames.map(name => anchor(
          `act:${loggableId}:${name}`,
          <div className="rounded-xl border border-border bg-card p-3">
            <div className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
              {nodeLabel}
            </div>
            <ActionCard runId={runId} loggableId={loggableId} name={name} showTimestamp />
          </div>,
        ))}
      {(typeFilter === 'all' || typeFilter === 'text') &&
        [...textsByName.entries()].map(([name, entries]) => anchor(
          `t:${loggableId}:${name}`,
          <TextFeedCard name={name} entries={entries} nodeLabel={nodeLabel} />,
        ))}
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
            // Fixed row height, natural width: label overlays align only
            // when the img box keeps the image's intrinsic aspect ratio,
            // so the old square object-cover crop is out.
            <button
              key={mediaEntryKey(e, i)}
              onClick={() => setLightbox(e)}
              className="relative h-24 shrink-0 overflow-hidden rounded-lg border border-border"
            >
              <ImageWithLabels
                src={api.mediaUrl(runId, e.mediaId)}
                labels={e.labels}
                loggableName={loggableId}
                imageName={e.name ?? ''}
                alt={e.name}
                imgClassName="block h-24 w-auto max-w-none"
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

function TextFeedCard({ name, entries, nodeLabel }: { name: string; entries: TextEntry[]; nodeLabel: string }) {
  const timelineFilter = useTimelineFilter()

  // The card renders at natural height inside the (scrolling) feed — no
  // inner scroller, so the tail is kept short. The node sheet has the
  // full stream.
  const visible = useMemo(() => {
    const filtered = timelineFilter ? entries.filter(e => timelineFilter.matchEntry(e)) : entries
    return filtered.slice(-30)
  }, [entries, timelineFilter])

  return (
    <div className="rounded-xl border border-border bg-card px-3.5 py-3">
      <div className="mb-2 flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[13.5px] font-medium">{name}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">{nodeLabel} · tail</span>
      </div>
      <div className="font-mono">
        {visible.length === 0 && (
          <div className="py-1 text-[11px] text-muted-foreground">No entries in current range</div>
        )}
        {visible.map((t, i) => (
          <div key={i} className="rounded px-1.5 py-0.5 text-[11px] leading-[1.45]">
            <span className="mr-1.5 text-muted-foreground">
              {formatTimestamp(t.timestamp)}
              {t.step != null ? ` · step ${t.step}` : ''}
            </span>
            {t.message}
          </div>
        ))}
      </div>
    </div>
  )
}

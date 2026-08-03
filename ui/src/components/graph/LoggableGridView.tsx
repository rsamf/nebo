import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion } from 'motion/react'
import { useStore, type ImageEntry, type AudioEntry } from '@/store'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import { Search } from 'lucide-react'
import { MetricBlock, ComparisonMetricBlock } from '@/components/node-tabs/NodeMetrics'
import { VirtualizedImageList, ComparisonImageCell } from '@/components/node-tabs/NodeImages'
import { AudioItem, ComparisonAudioCell } from '@/components/node-tabs/NodeAudio'
import { TextBlock, ComparisonTextCell } from '@/components/node-tabs/NodeText'
import { ComparisonGrid } from '@/components/shared/ComparisonGrid'
import { topologicalSort } from '@/lib/graph'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { useTimelineFilter } from '@/hooks/useTimelineFilter'
import { useContextMenu } from '@/hooks/useContextMenu'
import { GridCardContextMenu, type GridCardKind } from './GridCardContextMenu'
import { NAV_MODALITY_ORDER, type NavModality } from '@/lib/navTarget'
import type { LoggableMetricSeries, TextEntry } from '@/lib/api'

interface LoggableGridViewProps {
  runId: string
  // When set, the grid renders comparison cards: the union of loggables and
  // stream/metric names across the runs, with one comparison chart or
  // split-panel grid per card. `runId` stays the anchor (first) run used
  // for context-menu iframe URLs.
  comparisonRunIds?: string[]
}

// ─── Card chrome ─────────────────────────────────────────────────────────────

interface CardShellProps {
  title: string
  children: React.ReactNode
}

// Cards in the grid lock to a uniform height so the tile field stays
// even regardless of how much data each loggable carries. The body is a
// clipping box, NOT a scroller: each card body component owns exactly one
// full-height scroll container (the virtualized text/image list, the audio
// stack) so a card never nests same-axis scrollbars. Per-block Expand
// buttons inside metric/image headers give the user a larger viewport
// when they need it.
const CARD_HEIGHT_PX = 360

const CardShell = memo(function CardShell({ title, children }: CardShellProps) {
  return (
    <div
      className="border-2 border-border rounded-lg flex flex-col min-w-0"
      style={{ height: CARD_HEIGHT_PX }}
    >
      <div className="flex items-start gap-2 px-3 py-2 border-b border-border shrink-0">
        <span className="flex-1 min-w-0 text-sm font-medium truncate">{title}</span>
      </div>
      <div className="p-3 min-w-0 flex-1 min-h-0 overflow-hidden">{children}</div>
    </div>
  )
})

/**
 * Per-card right-click wrapper. Owns the context-menu state for one card
 * and renders the menu (currently a single "Copy iframe URL" entry).
 *
 * Lives at the card-wrapper layer rather than inside CardShell so the
 * `CardSpec` carrying the slice metadata (kind, loggableId, name) stays
 * out of CardShell's prop surface.
 */
function GridCardWrapper({ runId, card }: { runId: string; card: CardSpec }) {
  const contextMenu = useContextMenu()
  return (
    <>
      <div {...contextMenu.handlers}>
        <CardShell title={card.title}>{card.render()}</CardShell>
      </div>
      <GridCardContextMenu
        isOpen={contextMenu.isOpen}
        position={contextMenu.position}
        onClose={contextMenu.close}
        runId={runId}
        kind={card.kind}
        loggableId={card.loggableId}
        name={card.name}
      />
    </>
  )
}

// ─── Per-loggable card bodies (used by both Global and per-function rows) ────

function TextCardBody({ runId, loggableId, name, entries }: {
  runId: string
  loggableId: string
  name: string
  entries: TextEntry[]
}) {
  return <TextBlock name={name} entries={entries} runId={runId} loggableId={loggableId} fill />
}

// "No entries in current range" shown when the timeline tracker excludes
// every entry in this card. The card stays visible so the user can widen
// the tracker without losing track of which loggables actually exist.
function EmptyForRange() {
  return (
    <p className="text-xs text-muted-foreground">No entries in current range</p>
  )
}

function MetricCardBody({
  runId,
  loggableId,
  name,
  series,
}: {
  runId: string
  loggableId: string
  name: string
  series: LoggableMetricSeries
}) {
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  // The step tracker doesn't filter metric entries here. Line/scatter
  // are accumulating and their renderers mark the active step inline
  // (vertical guideline / dimmed non-matching points); bar/pie/histogram
  // are stepless snapshots. Filtering would only ever hide context.
  if (series.entries.length === 0) return <EmptyForRange />
  return <MetricBlock name={name} series={series} color={runColor} fill runId={runId} loggableId={loggableId} />
}

function ImageCardBody({
  runId,
  loggableId,
  entries,
}: {
  runId: string
  loggableId: string
  entries: ImageEntry[]
}) {
  const timelineFilter = useTimelineFilter()
  const visible = useMemo(() => {
    const out = timelineFilter ? entries.filter(e => timelineFilter.matchEntry(e)) : entries
    return out
  }, [entries, timelineFilter])
  if (visible.length === 0) return <EmptyForRange />
  // No pixel cap: the list fills the card body (the card's single scroller).
  return (
    <VirtualizedImageList
      runId={runId}
      loggableId={loggableId}
      images={visible}
      showTimestamp
    />
  )
}

function AudioCardBody({ runId, entries }: { runId: string; entries: AudioEntry[] }) {
  const timelineFilter = useTimelineFilter()
  const visible = useMemo(() => {
    const out = timelineFilter ? entries.filter(e => timelineFilter.matchEntry(e)) : entries
    return out
  }, [entries, timelineFilter])
  if (visible.length === 0) return <EmptyForRange />
  // The card body clips; this list is the card's single scroller.
  return (
    <div className="h-full overflow-auto">
      <div className="space-y-3">
        {visible.map(entry => (
          <AudioItem key={entry.mediaId} runId={runId} entry={entry} showTimestamp />
        ))}
      </div>
    </div>
  )
}

// ─── Tab + section model ────────────────────────────────────────────────────

type TabKey = 'text' | 'metrics' | 'images' | 'audio'

interface CardSpec {
  cardId: string                       // unique per (section, tab, name)
  title: string                        // "Section > Item" displayed in card header
  render: () => React.ReactNode
  // What this card represents — used by the right-click context menu
  // to build the matching iframe URL.
  kind: GridCardKind
  loggableId: string
  // The stream/metric/media name this card shows.
  name?: string
}

interface SectionSpec {
  /** Stable key — `__global__`, `__agent__`, or the loggable's id for function nodes. */
  sectionId: string
  /** Display name shown above the card grid; clickable to filter. */
  label: string
  cards: CardSpec[]
}

// Module-level fallbacks. zustand re-renders when a selector returns a
// fresh reference, so we share these to keep the rest of the component
// memo-stable when the underlying slice is undefined.
const EMPTY_METRICS: Record<string, LoggableMetricSeries> = {}
const EMPTY_IMAGES: ImageEntry[] = []
const EMPTY_AUDIO: AudioEntry[] = []
const EMPTY_TEXTS: TextEntry[] = []
const EMPTY_LOGGABLE_IMAGES: Record<string, ImageEntry[]> = {}
const EMPTY_LOGGABLE_AUDIO: Record<string, AudioEntry[]> = {}
const EMPTY_LOGGABLE_METRICS: Record<string, Record<string, LoggableMetricSeries>> = {}

// How long a deep-linked card stays flashed.
const HIGHLIGHT_MS = 3000

// Tab priority when a link names a loggable but no stream. Derived from
// the shared modality order so desktop and mobile resolve links the same.
const MODALITY_TO_TAB: Record<NavModality, TabKey> = {
  text: 'text', metric: 'metrics', image: 'images', audio: 'audio',
}
const TAB_ORDER: TabKey[] = NAV_MODALITY_ORDER.map(m => MODALITY_TO_TAB[m])

/**
 * Find the card a `nebo://` target addresses, and the tab that owns it.
 *
 * An exact `name` match wins wherever it lives (a stream name is unique
 * per loggable across modalities in practice, but text/metric/media are
 * separate namespaces — searching in TAB_ORDER makes ties deterministic).
 * A loggable-only link falls back to that loggable's first card.
 */
function resolveNavTarget(
  tabs: Record<TabKey, SectionSpec[]>,
  target: { loggableId: string; name: string | null },
): { tab: TabKey; cardId: string } | null {
  if (target.name) {
    for (const tab of TAB_ORDER) {
      for (const section of tabs[tab]) {
        if (section.sectionId !== target.loggableId) continue
        const card = section.cards.find(c => c.name === target.name)
        if (card) return { tab, cardId: card.cardId }
      }
    }
  }
  for (const tab of TAB_ORDER) {
    for (const section of tabs[tab]) {
      if (section.sectionId === target.loggableId && section.cards.length > 0) {
        return { tab, cardId: section.cards[0].cardId }
      }
    }
  }
  return null
}

// ─── Main view ───────────────────────────────────────────────────────────────

export function LoggableGridView({ runId, comparisonRunIds }: LoggableGridViewProps) {
  if (comparisonRunIds && comparisonRunIds.length > 0) {
    return <ComparisonGridCards runIds={comparisonRunIds} />
  }
  return <SingleRunGridCards runId={runId} />
}

function SingleRunGridCards({ runId }: { runId: string }) {
  const graph = useStore(s => s.runs.get(runId)?.graph)

  // Subscribe to raw slices only; derive groupings via useMemo.
  const allTextsRaw = useStore(s => s.runs.get(runId)?.texts)
  const allMetricsRaw = useStore(s => s.runs.get(runId)?.loggableMetrics)
  const allImagesRaw = useStore(s => s.runs.get(runId)?.loggableImages)
  const allAudioRaw = useStore(s => s.runs.get(runId)?.loggableAudio)
  const allTexts = allTextsRaw ?? EMPTY_TEXTS
  const allMetrics = allMetricsRaw ?? EMPTY_LOGGABLE_METRICS
  const allImages = allImagesRaw ?? EMPTY_LOGGABLE_IMAGES
  const allAudio = allAudioRaw ?? EMPTY_LOGGABLE_AUDIO

  // Section list: Global + Agent first, then function nodes in topo order.
  const sectionDescriptors = useMemo(() => {
    if (!graph) return [] as { sectionId: string; label: string }[]
    const fnIds = Object.keys(graph.nodes)
    const sortedFnIds = topologicalSort(fnIds, graph.edges)
    const sections: { sectionId: string; label: string }[] = [
      { sectionId: '__global__', label: 'Global' },
      { sectionId: '__agent__', label: 'Agent' },
    ]
    for (const id of sortedFnIds) {
      sections.push({
        sectionId: id,
        label: graph.nodes[id]?.func_name ?? id,
      })
    }
    return sections
  }, [graph])

  // Helper: text entries are a flat array on the run, indexed by node — so
  // for every section we filter once. Could be made a Map for O(N) instead
  // of O(N×sections), but section counts stay small in practice.
  const textsBySection = useMemo(() => {
    const m = new Map<string, typeof allTexts>()
    for (const s of sectionDescriptors) {
      m.set(s.sectionId, allTexts.filter(t => t.node === s.sectionId))
    }
    return m
  }, [allTexts, sectionDescriptors])

  // Build the four tabs' section specs. Each tab decides what counts as
  // a "card" for a given loggable.
  const tabs = useMemo<Record<TabKey, SectionSpec[]>>(() => {
    const text: SectionSpec[] = []
    const metrics: SectionSpec[] = []
    const images: SectionSpec[] = []
    const audio: SectionSpec[] = []

    for (const { sectionId, label } of sectionDescriptors) {
      // Text — one card per stream name on this loggable, mirroring how
      // metrics/images card up per name.
      const textEntries = textsBySection.get(sectionId) ?? []
      if (textEntries.length > 0) {
        const byName = new Map<string, TextEntry[]>()
        for (const t of textEntries) {
          const arr = byName.get(t.name) ?? []
          arr.push(t)
          byName.set(t.name, arr)
        }
        text.push({
          sectionId,
          label,
          cards: [...byName.entries()].map(([name, entries]) => ({
            cardId: `text:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <TextCardBody runId={runId} loggableId={sectionId} name={name} entries={entries} />
            ),
            kind: 'text' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      // Metrics — one card per metric name on this loggable.
      const loggableMetrics = allMetrics[sectionId] ?? EMPTY_METRICS
      const metricEntries = Object.entries(loggableMetrics)
      if (metricEntries.length > 0) {
        metrics.push({
          sectionId,
          label,
          cards: metricEntries.map(([name, series]) => ({
            cardId: `metric:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <MetricCardBody runId={runId} loggableId={sectionId} name={name} series={series} />
            ),
            kind: 'metric' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      // Images — group by image name so cards line up with how users
      // named their `nb.log_image(name=...)` calls.
      const loggableImages = allImages[sectionId] ?? EMPTY_IMAGES
      if (loggableImages.length > 0) {
        const byName = new Map<string, ImageEntry[]>()
        for (const img of loggableImages) {
          const arr = byName.get(img.name) ?? []
          arr.push(img)
          byName.set(img.name, arr)
        }
        images.push({
          sectionId,
          label,
          cards: [...byName.entries()].map(([name, entries]) => ({
            cardId: `image:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <ImageCardBody
                runId={runId}
                loggableId={sectionId}
                entries={entries}
              />
            ),
            kind: 'image' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      // Audio — same shape as images.
      const loggableAudio = allAudio[sectionId] ?? EMPTY_AUDIO
      if (loggableAudio.length > 0) {
        const byName = new Map<string, AudioEntry[]>()
        for (const a of loggableAudio) {
          const arr = byName.get(a.name) ?? []
          arr.push(a)
          byName.set(a.name, arr)
        }
        audio.push({
          sectionId,
          label,
          cards: [...byName.entries()].map(([name, entries]) => ({
            cardId: `audio:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => <AudioCardBody runId={runId} entries={entries} />,
            kind: 'audio' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }
    }

    return { text, metrics, images, audio }
  }, [runId, sectionDescriptors, textsBySection, allMetrics, allImages, allAudio])

  return <GridShell runId={runId} tabs={tabs} loading={!graph} />
}

// ─── Comparison cards ───────────────────────────────────────────────────────

/**
 * Comparison-mode card grid: same shell, but each card compares the runs —
 * metric cards render the shared comparison chart block (with the run-chip
 * row), text/image/audio cards render the split-panel ComparisonGrid with
 * one cell per run.
 */
function ComparisonGridCards({ runIds }: { runIds: string[] }) {
  const runs = useStore(s => s.runs)
  const runColors = useStore(s => s.runColors)
  const getOrAssignRunColor = useStore(s => s.getOrAssignRunColor)

  useEffect(() => {
    for (const rid of runIds) getOrAssignRunColor(rid)
  }, [runIds, getOrAssignRunColor])

  // Run chips on metric cards share one active set across the whole grid
  // (same semantics as ComparisonMetrics in the node tab). Only *newly
  // appearing* runs get auto-added, so a WS update can't resurrect a run
  // the user deselected.
  const [activeRuns, setActiveRuns] = useState<Set<string>>(() => new Set(runIds))
  const seenRuns = useRef<Set<string>>(new Set(runIds))
  useEffect(() => {
    const fresh = runIds.filter(rid => !seenRuns.current.has(rid))
    if (fresh.length === 0) return
    for (const rid of fresh) seenRuns.current.add(rid)
    setActiveRuns(prev => {
      const next = new Set(prev)
      for (const rid of fresh) next.add(rid)
      return next
    })
  }, [runIds])

  const anchorRunId = runIds[0]

  const runNameFor = useCallback(
    (rid: string) =>
      runs.get(rid)?.summary.run_name ||
      runs.get(rid)?.summary.script_path.split('/').pop() ||
      rid,
    [runs],
  )

  // Union of sections across the compared runs: Global + Agent, then each
  // run's function nodes in that run's topo order, first-seen wins.
  const sectionDescriptors = useMemo(() => {
    const sections: { sectionId: string; label: string }[] = [
      { sectionId: '__global__', label: 'Global' },
      { sectionId: '__agent__', label: 'Agent' },
    ]
    const seen = new Set(sections.map(s => s.sectionId))
    for (const rid of runIds) {
      const graph = runs.get(rid)?.graph
      if (!graph) continue
      for (const id of topologicalSort(Object.keys(graph.nodes), graph.edges)) {
        if (seen.has(id)) continue
        seen.add(id)
        sections.push({ sectionId: id, label: graph.nodes[id]?.func_name ?? id })
      }
    }
    return sections
  }, [runs, runIds])

  const effectiveRunIds = useMemo(
    () => runIds.filter(rid => activeRuns.has(rid)),
    [runIds, activeRuns],
  )

  const tabs = useMemo<Record<TabKey, SectionSpec[]>>(() => {
    const text: SectionSpec[] = []
    const metrics: SectionSpec[] = []
    const images: SectionSpec[] = []
    const audio: SectionSpec[] = []

    for (const { sectionId, label } of sectionDescriptors) {
      // Card names are unioned across runs so a stream that exists in only
      // one compared run still gets a card (the other runs' cells show
      // their empty state).
      const textNames: string[] = []
      const textSeen = new Set<string>()
      const metricTypes = new Map<string, string>()
      const imageNames: string[] = []
      const imageSeen = new Set<string>()
      const audioNames: string[] = []
      const audioSeen = new Set<string>()
      for (const rid of runIds) {
        const run = runs.get(rid)
        if (!run) continue
        for (const t of run.texts) {
          if (t.node !== sectionId || textSeen.has(t.name)) continue
          textSeen.add(t.name)
          textNames.push(t.name)
        }
        for (const [name, series] of Object.entries(run.loggableMetrics[sectionId] ?? {})) {
          if (!metricTypes.has(name)) metricTypes.set(name, series.type)
        }
        for (const img of run.loggableImages[sectionId] ?? []) {
          if (imageSeen.has(img.name)) continue
          imageSeen.add(img.name)
          imageNames.push(img.name)
        }
        for (const a of run.loggableAudio[sectionId] ?? []) {
          if (audioSeen.has(a.name)) continue
          audioSeen.add(a.name)
          audioNames.push(a.name)
        }
      }

      if (textNames.length > 0) {
        text.push({
          sectionId,
          label,
          cards: textNames.map(name => ({
            cardId: `text:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <ComparisonGrid runIds={runIds} fillParent>
                {(cellRunId) => (
                  <ComparisonTextCell runId={cellRunId} loggableId={sectionId} name={name} fillParent />
                )}
              </ComparisonGrid>
            ),
            kind: 'text' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      if (metricTypes.size > 0) {
        metrics.push({
          sectionId,
          label,
          cards: [...metricTypes.entries()].map(([name, type]) => ({
            cardId: `metric:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            // The block renders at natural height (chips + chart), so it
            // gets the card's single scroller around it.
            render: () => (
              <div className="h-full overflow-auto">
                <ComparisonMetricBlock
                  name={name}
                  type={type}
                  loggableId={sectionId}
                  runIds={effectiveRunIds}
                  comparisonRunIds={runIds}
                  activeRuns={activeRuns}
                  setActiveRuns={setActiveRuns}
                  runColors={runColors}
                  runNameFor={runNameFor}
                />
              </div>
            ),
            kind: 'metric' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      if (imageNames.length > 0) {
        images.push({
          sectionId,
          label,
          cards: imageNames.map(name => ({
            cardId: `image:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <ComparisonGrid runIds={runIds} fillParent>
                {(cellRunId) => (
                  <ComparisonImageCell runId={cellRunId} loggableId={sectionId} name={name} fillParent />
                )}
              </ComparisonGrid>
            ),
            kind: 'image' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }

      if (audioNames.length > 0) {
        audio.push({
          sectionId,
          label,
          cards: audioNames.map(name => ({
            cardId: `audio:${sectionId}:${name}`,
            title: `${label} > ${name}`,
            render: () => (
              <ComparisonGrid runIds={runIds} fillParent>
                {(cellRunId) => (
                  <ComparisonAudioCell runId={cellRunId} loggableId={sectionId} name={name} fillParent />
                )}
              </ComparisonGrid>
            ),
            kind: 'audio' as const,
            loggableId: sectionId,
            name,
          })),
        })
      }
    }

    return { text, metrics, images, audio }
  }, [runs, runIds, sectionDescriptors, effectiveRunIds, activeRuns, runColors, runNameFor])

  return <GridShell runId={anchorRunId} tabs={tabs} loading={!runs.get(anchorRunId)?.graph} />
}

// ─── Shared shell (tab strip, search, chips, card grid, deep-link nav) ──────

function GridShell({ runId, tabs, loading }: {
  runId: string
  tabs: Record<TabKey, SectionSpec[]>
  loading: boolean
}) {
  const [activeTab, setActiveTab] = useState<TabKey>('metrics')
  const [activeSectionId, setActiveSectionId] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  // Card flashed after a nebo:// deep link lands on it.
  const [highlightedCardId, setHighlightedCardId] = useState<string | null>(null)
  const pendingNavTarget = useStore(s => s.pendingNavTarget)
  const setPendingNavTarget = useStore(s => s.setPendingNavTarget)

  // Only show tab buttons that have at least one card on this run.
  const visibleTabs = useMemo(() => {
    const order: { key: TabKey; label: string }[] = [
      { key: 'text', label: 'Text' },
      { key: 'metrics', label: 'Metrics' },
      { key: 'images', label: 'Images' },
      { key: 'audio', label: 'Audio' },
    ]
    return order.filter(t => tabs[t.key].length > 0)
  }, [tabs])

  // Snap activeTab onto a visible tab whenever the current one disappears
  // (e.g., no more cards in the active tab). Pure derivation — no useEffect
  // needed because we only render against `effectiveTab`.
  const effectiveTab: TabKey =
    visibleTabs.find(t => t.key === activeTab)?.key ?? visibleTabs[0]?.key ?? 'text'

  // Section filter: a chip row above the grid narrows the flat card
  // list to one loggable. Dropping a section that no longer carries
  // any cards clears the filter so the user doesn't end up with an
  // empty pane.
  const sectionsForTab = tabs[effectiveTab]
  const sectionChips = useMemo(
    () => sectionsForTab.map(s => ({ sectionId: s.sectionId, label: s.label })),
    [sectionsForTab],
  )
  const flatCards = useMemo(() => {
    const q = search.trim().toLowerCase()
    const matchesQuery = (title: string) => q === '' || title.toLowerCase().includes(q)
    const out: CardSpec[] = []
    for (const section of sectionsForTab) {
      if (activeSectionId && section.sectionId !== activeSectionId) continue
      for (const card of section.cards) {
        if (matchesQuery(card.title)) out.push(card)
      }
    }
    return out
  }, [sectionsForTab, activeSectionId, search])

  // Resolve a pending nebo:// target: switch to the tab that owns the
  // card, drop any filter hiding it, scroll it into view, and flash it.
  // Runs after every render (no dep array) because the target's data may
  // still be hydrating when the link is clicked — each store update
  // re-renders and retries. All state writes happen inside the rAF
  // callback, never synchronously in the effect body.
  useEffect(() => {
    if (!pendingNavTarget) return
    const raf = requestAnimationFrame(() => {
      const target = resolveNavTarget(tabs, pendingNavTarget)
      if (!target) return // not ingested yet — retried on the next render
      if (search) setSearch('')
      if (activeSectionId) setActiveSectionId(null)
      if (effectiveTab !== target.tab) {
        setActiveTab(target.tab)
        return // card mounts on the next render; scroll then
      }
      const el = document.getElementById(`card-${target.cardId}`)
      if (!el) return
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      setHighlightedCardId(target.cardId)
      setPendingNavTarget(null)
    })
    return () => cancelAnimationFrame(raf)
  })

  // The flash is a one-shot ~3s cue, then the card goes back to normal.
  useEffect(() => {
    if (!highlightedCardId) return
    const t = setTimeout(() => setHighlightedCardId(null), HIGHLIGHT_MS)
    return () => clearTimeout(t)
  }, [highlightedCardId])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">Loading...</p>
      </div>
    )
  }

  if (visibleTabs.length === 0) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">No data yet</p>
      </div>
    )
  }

  return (
    <ScrollArea className="h-full">
      <div className="sticky top-0 z-10 bg-background/95 backdrop-blur-sm border-b border-border">
        {/* Top-level category tabs */}
        <div className="flex items-center px-3 pt-2 gap-1">
          {visibleTabs.map(t => (
            <button
              key={t.key}
              onClick={() => {
                setActiveTab(t.key)
                // Switching tabs clears any prior section focus so the
                // user starts from the full overview.
                setActiveSectionId(null)
              }}
              className={cn(
                'px-3 py-1.5 text-xs font-medium border-b-2 -mb-px transition-colors',
                effectiveTab === t.key
                  ? 'text-foreground border-primary'
                  : 'text-muted-foreground border-transparent hover:text-foreground',
              )}
            >
              {t.label}
              <span className="ml-1.5 text-[10px] text-muted-foreground/80">
                {tabs[t.key].reduce((acc, s) => acc + s.cards.length, 0)}
              </span>
            </button>
          ))}
        </div>

        {/* Search + section chips. The chip row replaces the old
            per-section banners — cards live in one flat grid below
            and the user narrows by clicking a chip. */}
        <div className="flex items-center gap-2 px-3 py-2">
          <div className="flex items-center gap-1 flex-1 bg-muted rounded-md px-2">
            <Search className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
            <input
              type="text"
              placeholder="Filter cards..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="bg-transparent border-none outline-none text-xs py-1.5 w-full"
            />
          </div>
        </div>
        {sectionChips.length > 0 && (
          <div className="flex flex-wrap items-center gap-1 px-3 pb-2">
            <button
              onClick={() => setActiveSectionId(null)}
              className={cn(
                'text-[10px] rounded px-2 py-0.5 border transition-colors',
                activeSectionId === null
                  ? 'bg-accent text-accent-foreground border-accent'
                  : 'bg-muted/40 text-muted-foreground border-border hover:bg-muted',
              )}
            >
              All
            </button>
            {sectionChips.map(chip => {
              const active = activeSectionId === chip.sectionId
              return (
                <button
                  key={chip.sectionId}
                  onClick={() => setActiveSectionId(active ? null : chip.sectionId)}
                  className={cn(
                    'text-[10px] rounded px-2 py-0.5 border transition-colors',
                    active
                      ? 'bg-accent text-accent-foreground border-accent'
                      : 'bg-muted/40 text-muted-foreground border-border hover:bg-muted',
                  )}
                >
                  {chip.label}
                </button>
              )
            })}
          </div>
        )}
      </div>

      <div className="px-3 py-3">
        {flatCards.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-muted-foreground">
            <p className="text-sm">{search ? 'No cards match the filter' : 'No cards in this view'}</p>
          </div>
        ) : (
          // Container-driven wrap: as many ≥380px columns as fit, so
          // resizing the view (window, side panels) reflows the cards.
          // min(...,100%) keeps a lone column from overflowing containers
          // narrower than the card minimum.
          <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(380px,100%),1fr))]">
            {(() => {
              const seenLoggables = new Set<string>()
              return flatCards.map(card => {
                const isFirst = !seenLoggables.has(card.loggableId)
                if (isFirst) seenLoggables.add(card.loggableId)
                const highlighted = highlightedCardId === card.cardId
                return (
                  // Outer div keeps the per-loggable anchor (the tracker's
                  // stream selection scrolls to it); the inner motion div
                  // is the per-card anchor deep links target.
                  <div key={card.cardId} id={isFirst ? `loggable-card-${card.loggableId}` : undefined}>
                    <motion.div
                      id={`card-${card.cardId}`}
                      className="rounded-lg"
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
                      <GridCardWrapper runId={runId} card={card} />
                    </motion.div>
                  </div>
                )
              })
            })()}
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '@/store'
import { useStreams } from '@/hooks/useStreams'
import { useAxisTransform } from '@/hooks/useAxisTransform'
import { useIsDesktop } from '@/hooks/useMediaQuery'
import { StreamTree } from './StreamTree'
import { TrackerControls, ModalityChips } from './TrackerControls'
import { TimelineRuler, TimelineRows } from './TimelineGrid'
import { generateTicks } from './ticks'
import { Input } from '@/components/ui/input'
import { flattenRows, STREAM_MODALITIES, type FlatRow, type StreamModality } from '@/lib/streams'

const HEIGHT_KEY = 'nebo_tracker_height'
const ROW_H = 22
const HEADER_H = 26
// Desktop header is taller: the tree column stacks the modality chips under
// the search field, and the ruler must match its height so rows stay aligned.
// Tall enough for the chips to wrap onto a second line, which they do once
// the tree column is narrow (it is capped at 15% of the tracker width).
const DESKTOP_HEADER_H = 64
const TREE_W = 220
const PAD = 12  // horizontal inset (px) so edge ticks/datapoints aren't clipped
// Derived from the shared list, never re-declared: a modality missing here
// is invisible in the tracker even though its chip renders, because
// `activeModalities` seeds from it.
const MODALITIES: StreamModality[] = STREAM_MODALITIES

function loadHeight(): number {
  const v = Number(localStorage.getItem(HEIGHT_KEY))
  return Number.isFinite(v) && v >= 120 ? v : 220
}

export function Tracker({ runId }: { runId: string }) {
  const timeline = useStore(s => s.timeline)
  const setSelectedStream = useStore(s => s.setSelectedStream)
  const setStep = useStore(s => s.setTimelineStep)
  const setTime = useStore(s => s.setTimelineTime)
  const isStep = timeline.mode === 'step'
  const isDesktop = useIsDesktop()

  const [height, setHeight] = useState(loadHeight)
  const heightRef = useRef(height)
  const [collapsed, setCollapsed] = useState(false)
  // A comparison group (`cmp:<ts>`) is not a key in `state.runs`, so
  // useStreams can't resolve it and the tracker would show "No data". Mirror
  // RunDetailView, which renders the first member run's graph/flat view in
  // comparison mode — the tracker scrubs that same run. Resolve off the prop
  // (not the store) so EmbeddedView, which passes a real run id, is unaffected.
  const comparisonGroups = useStore(s => s.comparisonGroups)
  const effectiveRunId = runId?.startsWith('cmp:')
    ? comparisonGroups.get(runId)?.runIds[0] ?? runId
    : runId
  // The stream model stays computed while collapsed: step navigation
  // (Ctrl/⌘+arrows, prev/next buttons, the step input) derives its domain
  // from it and must keep working with the panel collapsed. The model is
  // incrementally cached per run, so this is cheap.
  const model = useStreams(effectiveRunId, true)
  const [touching, setTouching] = useState(false)
  const [collapsedNodes, setCollapsedNodes] = useState<Set<string>>(() => new Set())
  const [query, setQuery] = useState('')
  const [activeModalities, setActiveModalities] = useState<Set<StreamModality>>(() => new Set(MODALITIES))

  // X domain from modality-visible leaves (independent of search/collapse so
  // the zoom range doesn't jump while typing or collapsing).
  const domainLeaves = useMemo(
    () => model.leaves.filter(l => activeModalities.has(l.modality)),
    [model.leaves, activeModalities],
  )
  const [min, max] = useMemo(() => {
    let lo = Infinity, hi = -Infinity
    for (const l of domainLeaves) {
      if (isStep) {
        if (l.minStep != null) lo = Math.min(lo, l.minStep)
        if (l.maxStep != null) hi = Math.max(hi, l.maxStep)
      } else {
        lo = Math.min(lo, l.minTime); hi = Math.max(hi, l.maxTime)
      }
    }
    if (lo === Infinity) { lo = 0; hi = 0 }
    return [lo, hi]
  }, [domainLeaves, isStep])
  const minTime = useMemo(() => {
    let m = Infinity
    for (const l of domainLeaves) m = Math.min(m, l.minTime)
    return m === Infinity ? 0 : m
  }, [domainLeaves])

  const axis = useAxisTransform(min, max, PAD)

  // Desktop: one flattened row list (branches + leaves) drives BOTH the tree
  // column and the canvas, so they render identical rows in one shared scroll.
  const treeRows = useMemo(
    () => flattenRows(model.tree, collapsedNodes, query, activeModalities),
    [model.tree, collapsedNodes, query, activeModalities],
  )
  // Mobile: no tree column — a flat list of leaf streams (full path shown on
  // the canvas), filtered by modality + search, sorted by path.
  const mobileRows = useMemo<FlatRow[]>(() => {
    const q = query.trim().toLowerCase()
    return model.leaves
      .filter(l => activeModalities.has(l.modality) && (!q || l.path.toLowerCase().includes(q)))
      .sort((a, b) => a.path.localeCompare(b.path))
      .map(l => ({ key: l.path, label: l.path, path: l.path, depth: 0, isLeaf: true, leaf: l }))
  }, [model.leaves, activeModalities, query])
  const rows = isDesktop ? treeRows : mobileRows

  const range = max - min
  const ticks = useMemo(() => {
    if (range <= 0) return []
    const [a, b] = axis.visibleRange
    const vMin = min + (a / 100) * range
    const vMax = min + (b / 100) * range
    const raw = generateTicks(Math.max(min, vMin), Math.min(max, vMax))
    return isStep ? raw.map(Math.round) : raw
  }, [min, max, range, isStep, axis.visibleRange])

  const playhead = isStep ? timeline.step : timeline.time
  const playheadPct = playhead != null && range > 0 ? axis.toPercent(playhead) : null

  const onResetZoom = useCallback(() => axis.reset(), [axis])
  const onClearFilters = useCallback(() => {
    setStep(null)
    setTime(null)
    setSelectedStream(null)
    setQuery('')
    setActiveModalities(new Set(MODALITIES))
  }, [setStep, setTime, setSelectedStream])
  const toggleModality = useCallback((m: StreamModality) => setActiveModalities(prev => {
    const next = new Set(prev); if (next.has(m)) next.delete(m); else next.add(m); return next
  }), [])
  const onToggleNode = useCallback((path: string) => setCollapsedNodes(prev => {
    const next = new Set(prev); if (next.has(path)) next.delete(path); else next.add(path); return next
  }), [])

  // Selecting a stream highlights it and scrolls the main view to the owning
  // loggable card — it does NOT filter the content panels.
  const onSelect = useCallback((path: string) => {
    const next = timeline.selectedStream === path ? null : path
    setSelectedStream(next)
    const leaf = model.byPath.get(path)
    if (next && leaf) document.getElementById(`loggable-card-${leaf.loggableId}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [timeline.selectedStream, setSelectedStream, model.byPath])

  // Drag-to-resize the panel height.
  const dragging = useRef(false)
  const onHandleDown = (e: React.PointerEvent) => { dragging.current = true; (e.target as HTMLElement).setPointerCapture(e.pointerId) }
  const onHandleMove = (e: React.PointerEvent) => {
    if (!dragging.current) return
    const h = Math.max(120, Math.min(window.innerHeight * 0.7, window.innerHeight - e.clientY))
    heightRef.current = h
    setHeight(h)
  }
  const onHandleUp = () => { if (dragging.current) { dragging.current = false; localStorage.setItem(HEIGHT_KEY, String(heightRef.current)) } }

  // Scrub the playhead (left-drag) / pan (middle-drag) on the canvas.
  const gridRef = useRef<HTMLDivElement | null>(null)
  const scrubbing = useRef(false)
  const setGrid = useCallback((el: HTMLDivElement | null) => { gridRef.current = el; axis.setContainer(el) }, [axis])
  const fromPixel = useCallback((clientX: number) => {
    const el = gridRef.current
    if (!el || range <= 0) return min
    const rect = el.getBoundingClientRect()
    const innerLeft = rect.left + PAD
    const innerWidth = Math.max(1, rect.width - 2 * PAD)
    const trackX = (clientX - innerLeft - axis.panX) / axis.scale
    const frac = Math.max(0, Math.min(1, trackX / innerWidth))
    const v = min + frac * range
    return isStep ? Math.round(v) : v
  }, [min, range, isStep, axis.panX, axis.scale])
  const commitScrub = useCallback((clientX: number) => {
    const v = fromPixel(clientX); if (isStep) setStep(v); else setTime(v)
  }, [fromPixel, isStep, setStep, setTime])
  // pointermove fires faster than the display refreshes, and every commit
  // re-renders every step-subscribed chart. Coalesce moves to one trailing
  // commit per animation frame; pointer-down and release commit directly.
  const scrubPendingX = useRef<number | null>(null)
  const scrubRaf = useRef(0)
  const scrubThrottled = useCallback((clientX: number) => {
    scrubPendingX.current = clientX
    if (scrubRaf.current) return
    scrubRaf.current = requestAnimationFrame(() => {
      scrubRaf.current = 0
      if (scrubPendingX.current != null) commitScrub(scrubPendingX.current)
    })
  }, [commitScrub])
  useEffect(() => () => cancelAnimationFrame(scrubRaf.current), [])
  const onCanvasDown = (e: React.PointerEvent<HTMLDivElement>) => {
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* capture is best-effort */ }
    setTouching(true)
    if (e.button === 1) axis.beginPan(e.clientX)
    else { scrubbing.current = true; commitScrub(e.clientX) }
  }
  const onCanvasMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (scrubbing.current) scrubThrottled(e.clientX); else axis.onPanMove(e.clientX)
  }
  const onCanvasUp = () => {
    if (scrubbing.current && scrubPendingX.current != null) {
      cancelAnimationFrame(scrubRaf.current)
      scrubRaf.current = 0
      commitScrub(scrubPendingX.current)
      scrubPendingX.current = null
    }
    scrubbing.current = false; setTouching(false); axis.endPan()
  }

  // The step domain is computed independently of the active mode: step
  // navigation and playback must stay reachable from Time mode, since
  // both flip the tracker into Step mode themselves.
  const [minStep, maxStep] = useMemo(() => {
    if (isStep) return [min, max]
    let lo = Infinity, hi = -Infinity
    for (const l of domainLeaves) {
      if (l.minStep != null) lo = Math.min(lo, l.minStep)
      if (l.maxStep != null) hi = Math.max(hi, l.maxStep)
    }
    return lo === Infinity ? [0, 0] : [lo, hi]
  }, [isStep, min, max, domainLeaves])
  const hasSteps = maxStep > minStep

  return (
    <div className="shrink-0 border-t border-border bg-background flex flex-col" style={collapsed ? undefined : { height }}>
      {/* resize handle (only when expanded) */}
      {!collapsed && (
        <div
          className="h-1 w-full shrink-0 cursor-ns-resize hover:bg-primary/40"
          onPointerDown={onHandleDown} onPointerMove={onHandleMove} onPointerUp={onHandleUp}
        />
      )}
      {/* Controls row is always visible so the collapse toggle stays reachable. */}
      <TrackerControls
        minStep={minStep} maxStep={maxStep} hasSteps={hasSteps}
        activeModalities={activeModalities} onToggleModality={toggleModality}
        onResetZoom={onResetZoom} onClearFilters={onClearFilters}
        query={query} onQueryChange={setQuery}
        collapsed={collapsed} onToggleCollapse={() => setCollapsed(c => !c)}
      />
      {/* items-start so columns size to their CONTENT height (not stretched to
          the visible height); otherwise their content overflows the box — the
          tree border-r stops short and the sticky ruler unsticks once you
          scroll past one viewport. min-h-full keeps them filling the panel when
          there are few streams. */}
      {!collapsed && (
        <div className="flex flex-1 items-start overflow-x-hidden overflow-y-auto">
          {/* Tree column (desktop only) — sticky search header + rows. Capped at
              15% of the tracker width so it never dominates. On mobile the tree
              is hidden and stream names are shown flat on the canvas instead. */}
          {isDesktop && (
            <div className="min-h-full shrink-0 border-r border-border" style={{ width: TREE_W, maxWidth: '15%' }}>
              <div className="sticky top-0 z-10 space-y-1 border-b border-border bg-background p-1" style={{ height: DESKTOP_HEADER_H }}>
                <Input placeholder="Search streams…" value={query} onChange={e => setQuery(e.target.value)} className="h-[18px] text-[11px]" />
                <ModalityChips activeModalities={activeModalities} onToggleModality={toggleModality} />
              </div>
              <StreamTree
                rows={treeRows} rowHeight={ROW_H} collapsed={collapsedNodes}
                selectedPath={timeline.selectedStream} onSelect={onSelect} onToggle={onToggleNode}
              />
            </div>
          )}
          {/* Canvas column — sticky ruler + rows. Uses overflow-x:clip (NOT
              hidden) to clip the zoom transform: `hidden` on one axis forces
              the other to `auto`, which would make this column its own
              vertical scroller (breaking shared scroll + ruler stickiness).
              `clip` leaves overflow-y visible so the single outer container
              scrolls both columns together. */}
          <div
            ref={setGrid}
            className="relative min-h-full flex-1 cursor-crosshair select-none overflow-x-clip"
            onPointerDown={onCanvasDown} onPointerMove={onCanvasMove} onPointerUp={onCanvasUp} onPointerLeave={onCanvasUp}
          >
            {range <= 0 ? (
              <div className="sticky top-0 flex h-full items-center justify-center text-[11px] text-muted-foreground">
                {isStep ? 'No step data' : 'No time data'}
              </div>
            ) : (
              <>
                <TimelineRuler ticks={ticks} axis={axis} isStep={isStep} minTime={minTime} height={isDesktop ? DESKTOP_HEADER_H : HEADER_H} pad={PAD} playheadPct={playheadPct} />
                <TimelineRows rows={rows} rowHeight={ROW_H} isStep={isStep} axis={axis} ticks={ticks} pad={PAD} playheadPct={playheadPct} showLabels={!isDesktop} labelsDimmed={touching} />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

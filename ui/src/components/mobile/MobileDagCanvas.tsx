import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import dagre from '@dagrejs/dagre'
import { useStore } from '@/store'
import type { AudioEntry, ImageEntry } from '@/store'
import { api, type LoggableMetricSeries } from '@/lib/api'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { MetricPreview } from './MetricPreview'
import { ImageWithLabels } from '@/components/shared/ImageWithLabels'
import { firstSeriesFor, nearestAtStep } from './util'

// Touch-first DAG canvas: dagre layout, one-finger pan, two-finger
// pinch zoom, +/− buttons, tap a node for its detail sheet. Deliberately
// not ReactFlow — desktop node cards are illegible at mobile fit-zoom,
// and the mobile cards are fixed-size touch targets instead.

const NODE_W = 230
const BASE_H = 58
const PROGRESS_H = 15
const MIN_SCALE = 0.3
const MAX_SCALE = 2.5
// Movement beyond this many px means the gesture was a pan, not a tap.
const TAP_SLOP_PX = 8

// One preview per node, first available in this order.
type NodePreview =
  | { kind: 'image'; image: ImageEntry }
  | { kind: 'audio'; audio: AudioEntry }
  | { kind: 'metric'; series: LoggableMetricSeries }
  | { kind: 'text'; line: string }
  | null

const PREVIEW_HEIGHT: Record<'image' | 'audio' | 'metric' | 'text', number> = {
  image: 104,
  audio: 44,
  metric: 42,
  text: 34,
}

interface LaidNode {
  id: string
  x: number
  y: number
  height: number
  name: string
  sub: string
  exec: number
  preview: NodePreview
  progress: { current: number; total: number } | null
}

export function MobileDagCanvas({
  runId,
  onNodeTap,
}: {
  runId: string
  onNodeTap: (loggableId: string) => void
}) {
  const run = useStore(s => s.runs.get(runId))
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  // Image/audio previews follow the playhead (null reads as step 0), so
  // scrubbing the tracker pages the DAG's media through the run.
  // Node previews are media thumbnails — one fetch each — so like the
  // media panels they pin to where playback started rather than chasing
  // a 30 fps playhead. See useTimelineFilter.
  const playing = useStore(s => s.timeline.playing)
  const playbackFrom = useStore(s => s.timeline.playbackFrom)
  const liveStep = useStore(s => s.timeline.step)
  const timelineStep = playing ? playbackFrom : liveStep
  const graph = run?.graph
  const loggableMetrics = run?.loggableMetrics

  // Keyed on the run object: every store mutation clones the run, so
  // this recomputes exactly when this run (or the playhead) changes.
  const layout = useMemo(() => {
    if (!graph || Object.keys(graph.nodes).length === 0) return null

    // One backward walk over the (possibly 10k+) text array shared by all
    // nodes that fall through to a text-line preview, instead of a scan
    // per node.
    const lastTextLines = new Map<string, string>()
    const resolveTextLines = (ids: Set<string>) => {
      const texts = run?.texts ?? []
      for (let i = texts.length - 1; i >= 0 && ids.size > 0; i--) {
        const node = texts[i].node
        if (node && ids.has(node)) {
          lastTextLines.set(node, texts[i].message)
          ids.delete(node)
        }
      }
    }
    const needTexts = new Set<string>()
    for (const id of Object.keys(graph.nodes)) {
      if (
        !run?.loggableImages[id]?.length &&
        !run?.loggableAudio[id]?.length &&
        !firstSeriesFor(loggableMetrics, id)
      ) {
        needTexts.add(id)
      }
    }
    if (needTexts.size > 0) resolveTextLines(needTexts)

    const previewFor = (id: string): NodePreview => {
      const image = nearestAtStep(run?.loggableImages[id], timelineStep)
      if (image) return { kind: 'image', image }
      const audio = nearestAtStep(run?.loggableAudio[id], timelineStep)
      if (audio) return { kind: 'audio', audio }
      const series = firstSeriesFor(loggableMetrics, id)
      if (series) return { kind: 'metric', series }
      const line = lastTextLines.get(id)
      if (line) return { kind: 'text', line }
      return null
    }
    const g = new dagre.graphlib.Graph()
    g.setGraph({ rankdir: 'TB', nodesep: 28, ranksep: 52, marginx: 24, marginy: 24 })
    g.setDefaultEdgeLabel(() => ({}))
    const metas: Record<string, Omit<LaidNode, 'x' | 'y'>> = {}
    for (const [id, n] of Object.entries(graph.nodes)) {
      const preview = previewFor(id)
      const progress =
        n.progress && n.progress.total > 0 && n.progress.current < n.progress.total
          ? { current: n.progress.current, total: n.progress.total }
          : null
      const height =
        BASE_H +
        (preview ? PREVIEW_HEIGHT[preview.kind] : 0) +
        (progress ? PROGRESS_H : 0)
      metas[id] = {
        id,
        height,
        name: n.name,
        sub: n.docstring?.split('\n')[0] || n.func_name || id,
        exec: n.exec_count,
        preview,
        progress,
      }
      g.setNode(id, { width: NODE_W, height })
    }
    for (const e of graph.edges) {
      if (metas[e.source] && metas[e.target]) g.setEdge(e.source, e.target)
    }
    dagre.layout(g)
    const nodes: LaidNode[] = Object.keys(metas).map(id => {
      const pos = g.node(id)
      return { ...metas[id], x: pos.x - NODE_W / 2, y: pos.y - metas[id].height / 2 }
    })
    const byId = new Map(nodes.map(n => [n.id, n]))
    const edges = graph.edges
      .filter(e => byId.has(e.source) && byId.has(e.target))
      .map(e => {
        const s = byId.get(e.source)!
        const t = byId.get(e.target)!
        const sx = s.x + NODE_W / 2
        const sy = s.y + s.height
        const tx = t.x + NODE_W / 2
        const ty = t.y
        const my = (sy + ty) / 2
        return { key: `${e.source}→${e.target}`, d: `M ${sx} ${sy} C ${sx} ${my}, ${tx} ${my}, ${tx} ${ty}` }
      })
    let w = 0
    let h = 0
    for (const n of nodes) {
      w = Math.max(w, n.x + NODE_W + 24)
      h = Math.max(h, n.y + n.height + 24)
    }
    return { nodes, edges, width: w, height: h }
    // graph/loggableMetrics are reachable from `run`; listed for clarity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, timelineStep])

  // Pan/zoom via plain setState per pointer event — mobile graphs are
  // small enough that a React render per frame is fine.
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 })
  const containerRef = useRef<HTMLDivElement | null>(null)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const pinchDist = useRef<number | null>(null)
  const movedRef = useRef(false)
  const downPos = useRef({ x: 0, y: 0 })
  const fittedFor = useRef<string | null>(null)

  // Initial fit: scale the graph's width into the viewport, but never
  // below a readable floor — illegible fit-zoom was the baseline's pain
  // point, so very wide graphs start readable and pan instead.
  // A zero-width container (hidden tab, iframe that hasn't laid out yet)
  // can't be fitted; a ResizeObserver retries once real bounds arrive —
  // for a finished run `layout` never recomputes, so without the retry
  // the canvas would stay stuck at scale 1 / origin 0,0.
  useEffect(() => {
    if (!layout || fittedFor.current === runId) return
    const el = containerRef.current
    if (!el) return
    const fit = () => {
      if (fittedFor.current === runId) return true
      const vw = el.clientWidth
      if (vw <= 0) return false
      const scale = Math.max(0.55, Math.min(1, (vw - 16) / layout.width))
      setView({ x: Math.min(8, (vw - layout.width * scale) / 2), y: 12, scale })
      fittedFor.current = runId
      return true
    }
    if (fit()) return
    const ro = new ResizeObserver(() => {
      if (fit()) ro.disconnect()
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [layout, runId])

  const clampScale = (s: number) => Math.max(MIN_SCALE, Math.min(MAX_SCALE, s))

  const zoomAround = useCallback((cx: number, cy: number, factor: number) => {
    setView(v => {
      const scale = clampScale(v.scale * factor)
      const k = scale / v.scale
      return { scale, x: cx - (cx - v.x) * k, y: cy - (cy - v.y) * k }
    })
  }, [])

  const zoomButton = (factor: number) => {
    const el = containerRef.current
    if (!el) return
    zoomAround(el.clientWidth / 2, el.clientHeight / 2, factor)
  }

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // No explicit pointer capture: capturing retargets the derived click
    // to the container, which would swallow node taps. Touch pointers
    // implicitly capture to the child they land on and still bubble here.
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointers.current.size === 1) {
      movedRef.current = false
      downPos.current = { x: e.clientX, y: e.clientY }
    } else if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]
      pinchDist.current = Math.hypot(a.x - b.x, a.y - b.y)
    }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const prev = pointers.current.get(e.pointerId)
    if (!prev) return
    const cur = { x: e.clientX, y: e.clientY }
    pointers.current.set(e.pointerId, cur)
    if (
      Math.hypot(cur.x - downPos.current.x, cur.y - downPos.current.y) > TAP_SLOP_PX
    ) {
      movedRef.current = true
    }
    if (pointers.current.size === 1) {
      setView(v => ({ ...v, x: v.x + cur.x - prev.x, y: v.y + cur.y - prev.y }))
    } else if (pointers.current.size === 2 && pinchDist.current != null) {
      const [a, b] = [...pointers.current.values()]
      const dist = Math.hypot(a.x - b.x, a.y - b.y)
      if (dist > 0 && pinchDist.current > 0) {
        const rect = containerRef.current?.getBoundingClientRect()
        const mx = (a.x + b.x) / 2 - (rect?.left ?? 0)
        const my = (a.y + b.y) / 2 - (rect?.top ?? 0)
        zoomAround(mx, my, dist / pinchDist.current)
      }
      pinchDist.current = dist
    }
  }

  const onPointerEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(e.pointerId)
    if (pointers.current.size < 2) pinchDist.current = null
  }

  // A node tap only fires when the gesture wasn't a pan/pinch.
  const tapNode = (id: string) => {
    if (!movedRef.current) onNodeTap(id)
  }

  if (!layout) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        No DAG yet — waiting for the first @nb.fn() call
      </div>
    )
  }

  return (
    <div
      ref={containerRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
      onPointerLeave={onPointerEnd}
      className="relative h-full touch-none select-none overflow-hidden"
      style={{
        backgroundImage: 'radial-gradient(var(--color-border) 1px, transparent 1px)',
        backgroundSize: '20px 20px',
      }}
    >
      <div
        className="absolute left-0 top-0"
        style={{
          width: layout.width,
          height: layout.height,
          transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`,
          transformOrigin: '0 0',
        }}
      >
        <svg
          width={layout.width}
          height={layout.height}
          className="absolute inset-0"
          fill="none"
          stroke="var(--color-muted-foreground)"
          strokeWidth={1.5}
          opacity={0.6}
        >
          {layout.edges.map(e => (
            <path key={e.key} d={e.d} />
          ))}
        </svg>
        {layout.nodes.map(n => (
          <div
            key={n.id}
            onClick={() => tapNode(n.id)}
            className="absolute cursor-pointer rounded-[14px] border-[1.5px] bg-card px-3.5 py-3"
            style={{
              left: n.x,
              top: n.y,
              width: NODE_W,
              borderColor: n.progress ? '#3b82f6' : 'var(--color-border)',
              boxShadow: n.progress ? '0 0 16px rgba(59, 130, 246, 0.25)' : undefined,
            }}
          >
            <div className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-sm font-semibold">{n.name}</span>
              <span className="shrink-0 text-[11px] text-muted-foreground">×{n.exec}</span>
            </div>
            <div className="mt-0.5 truncate text-[11px] text-muted-foreground">{n.sub}</div>
            {n.preview?.kind === 'image' && (
              // Centered at intrinsic aspect ratio (capped to the node
              // width and preview height) so the label overlays align —
              // they stretch over the img box, which object-cover breaks.
              <div className="mt-2 text-center">
                <div className="relative inline-block max-w-full">
                  <ImageWithLabels
                    key={n.preview.image.mediaId}
                    src={api.mediaUrl(runId, n.preview.image.mediaId)}
                    labels={n.preview.image.labels}
                    loggableName={n.id}
                    imageName={n.preview.image.name ?? ''}
                    alt={n.preview.image.name}
                    imgClassName="block max-h-24 max-w-full rounded-lg border border-border"
                  />
                  {n.preview.image.step != null && (
                    <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1 font-mono text-[9px] text-white">
                      s{n.preview.image.step}
                    </span>
                  )}
                </div>
              </div>
            )}
            {n.preview?.kind === 'audio' && (
              <div className="mt-2" onClick={e => e.stopPropagation()}>
                <audio
                  controls
                  preload="none"
                  src={api.mediaUrl(runId, n.preview.audio.mediaId)}
                  className="h-9 w-full"
                />
              </div>
            )}
            {n.preview?.kind === 'metric' && (
              <MetricPreview
                series={n.preview.series}
                color={runColor}
                width={NODE_W - 30}
                height={34}
                className="mt-2"
              />
            )}
            {n.preview?.kind === 'text' && (
              <div className="mt-2 line-clamp-2 font-mono text-[10px] leading-snug text-muted-foreground">
                {n.preview.line}
              </div>
            )}
            {n.progress && (
              <div className="mt-2 h-[5px] overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-blue-500"
                  style={{ width: `${Math.min(100, (n.progress.current / n.progress.total) * 100)}%` }}
                />
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="absolute right-3 top-3 flex flex-col gap-1.5">
        <button
          onClick={() => zoomButton(1.25)}
          aria-label="Zoom in"
          className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-lg"
        >
          +
        </button>
        <button
          onClick={() => zoomButton(1 / 1.25)}
          aria-label="Zoom out"
          className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-card text-lg"
        >
          −
        </button>
      </div>
    </div>
  )
}

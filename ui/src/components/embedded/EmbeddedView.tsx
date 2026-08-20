import { useEffect, useMemo, useState } from 'react'
import { useStore } from '@/store'
import { useRunData } from '@/hooks/useRunData'
import { useIsDesktop } from '@/hooks/useMediaQuery'
import { resolveNodeRef, type EmbeddedView as EmbeddedSpec } from '@/hooks/useEmbeddedView'
import { DagGraph } from '@/components/graph/DagGraph'
import { LoggableGridView } from '@/components/graph/LoggableGridView'
import { MobileDagCanvas } from '@/components/mobile/MobileDagCanvas'
import { MobileFeed } from '@/components/mobile/MobileFeed'
import { MobileTracker } from '@/components/mobile/MobileTracker'
import { MobileNodeSheet } from '@/components/mobile/MobileNodeSheet'
import { LoggableTabContainer } from '@/components/node-tabs/LoggableTabContainer'
import { MetricBlock } from '@/components/node-tabs/NodeMetrics'
import { ImageItem } from '@/components/node-tabs/NodeImages'
import { AudioItem } from '@/components/node-tabs/NodeAudio'
import { ActionCard } from '@/components/actions/ActionCard'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { ConfigChips } from '@/components/shared/ConfigChips'
import { ScrollArea } from '@/components/ui/scroll-area'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { mediaEntryKey } from '@/lib/utils'
import { Tracker } from '@/components/timeline/Tracker'

// Media galleries cap at the most recent N entries — embeds live in
// iframes and must stay light even against 10k-step runs.
const MAX_EMBED_MEDIA_ITEMS = 200

/**
 * Top-level dispatcher for `?view=<kind>&run=<id>...` URLs. Renders only the
 * requested slice of a run with no sidebar / app header — designed for
 * iframe embeds.
 */
export function EmbeddedView({ spec }: { spec: EmbeddedSpec }) {
  // Make sure the requested run is the selected one so existing components
  // that read selectedRunId behave consistently inside the embed.
  const selectRun = useStore(s => s.selectRun)
  useEffect(() => {
    selectRun(spec.runId)
  }, [spec.runId, selectRun])

  const run = useRunData(spec.runId)
  const isDesktop = useIsDesktop()

  if (!run) {
    return (
      <div className="flex items-center justify-center h-screen text-muted-foreground">
        <p className="text-sm">Loading run…</p>
      </div>
    )
  }

  switch (spec.kind) {
    // The layout kinds have dedicated mobile renderings — a phone-width
    // iframe (or a phone) gets the touch DAG/feed/tracker, not a shrunk
    // ReactFlow canvas. Panel kinds below are layout-neutral either way.
    case 'run':
    case 'dag':
    case 'flat':
      if (!isDesktop) return <EmbeddedMobileLayout runId={spec.runId} kind={spec.kind} />
      if (spec.kind === 'dag') return <EmbeddedNodes runId={spec.runId} />
      if (spec.kind === 'flat') return <EmbeddedGrid runId={spec.runId} />
      return <EmbeddedRun runId={spec.runId} />
    case 'node':
      return <EmbeddedNode spec={spec} />
    case 'text':
      return <EmbeddedText spec={spec} />
    // Plural (gallery) and singular (filtered) share a renderer; the
    // component already narrows by `spec.name` when set.
    case 'metrics':
    case 'metric':
      return <EmbeddedMetrics spec={spec} />
    case 'images':
    case 'image':
      return <EmbeddedImages spec={spec} />
    case 'audios':
    case 'audio':
      return <EmbeddedAudio spec={spec} />
    case 'actions':
    case 'action':
      return <EmbeddedActions spec={spec} />
  }
}

function EmbeddedRun({ runId }: { runId: string }) {
  return (
    <div className="flex flex-col h-screen">
      <div className="flex-1 overflow-hidden">
        <DagGraph runId={runId} />
      </div>
      <Tracker runId={runId} />
    </div>
  )
}

// Phone-width rendering of the layout embed kinds. 'run' is the mobile
// app's run-view body: DAG ⇄ Feed (toggled from the tracker bar, seeded
// by the shared viewMode) over the heat-strip tracker. 'dag' and 'flat'
// pin one view with no tracker. Node taps open the node sheet.
function EmbeddedMobileLayout({ runId, kind }: { runId: string; kind: 'run' | 'dag' | 'flat' }) {
  const [nodeSheet, setNodeSheet] = useState<string | null>(null)
  const viewMode = useStore(s => s.viewMode)
  const showDag = kind === 'dag' || (kind === 'run' && viewMode === 'graph')

  return (
    <div className="flex h-screen flex-col">
      <div className="min-h-0 flex-1 overflow-hidden">
        {showDag ? (
          <MobileDagCanvas runId={runId} onNodeTap={setNodeSheet} />
        ) : (
          <MobileFeed runId={runId} />
        )}
      </div>
      {kind === 'run' && <MobileTracker runId={runId} />}
      <MobileNodeSheet runId={runId} loggableId={nodeSheet} onClose={() => setNodeSheet(null)} />
    </div>
  )
}

function EmbeddedNodes({ runId }: { runId: string }) {
  return (
    <div className="h-screen">
      <DagGraph runId={runId} />
    </div>
  )
}

function EmbeddedGrid({ runId }: { runId: string }) {
  return (
    <div className="h-screen">
      <LoggableGridView runId={runId} />
    </div>
  )
}

function EmbeddedNode({ spec }: { spec: EmbeddedSpec }) {
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const nodeId = useMemo(
    () => resolveNodeRef(spec.nodeRef, graph?.nodes),
    [spec.nodeRef, graph?.nodes],
  )
  const node = nodeId ? graph?.nodes[nodeId] : null

  if (!nodeId || !node) {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        Node not found: <code>{spec.nodeRef ?? '(no node param)'}</code>
      </div>
    )
  }

  return (
    <ScrollArea className="h-screen">
      <div className="border-2 border-border rounded-lg m-3">
        <div className="px-3 py-2.5 border-b border-border">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium truncate">{node.func_name}</span>
            {node.exec_count > 0 && (
              <span className="text-xs text-muted-foreground shrink-0">
                x{node.exec_count.toLocaleString()}
              </span>
            )}
          </div>
          {node.docstring && (
            <Tooltip>
              <TooltipTrigger asChild>
                <p className="text-xs text-muted-foreground mt-0.5 line-clamp-1 cursor-help">
                  {node.docstring.split('\n')[0]}
                </p>
              </TooltipTrigger>
              <TooltipContent side="top" align="start">{node.docstring}</TooltipContent>
            </Tooltip>
          )}
          <ConfigChips params={node.params} />
        </div>
        <div className="p-3">
          <LoggableTabContainer runId={spec.runId} loggableId={nodeId} />
        </div>
      </div>
    </ScrollArea>
  )
}

function EmbeddedText({ spec }: { spec: EmbeddedSpec }) {
  const textsRaw = useStore(s => s.runs.get(spec.runId)?.texts)
  const texts = textsRaw ?? EMPTY_TEXTS
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const filterNodeId = useMemo(
    () => resolveNodeRef(spec.nodeRef, graph?.nodes),
    [spec.nodeRef, graph?.nodes],
  )
  const filtered = texts
    .filter(t => (filterNodeId ? t.node === filterNodeId : true))
    .filter(t => (spec.name ? t.name === spec.name : true))

  return (
    <ScrollArea className="h-screen">
      <div className="font-mono text-xs p-3 space-y-0.5">
        {filtered.length === 0 && (
          <p className="text-muted-foreground">No text</p>
        )}
        {filtered.map((t, i) => (
          <div key={i}>
            <span className="text-muted-foreground mr-2">
              {t.node && `[${t.node}] `}{t.name}{t.step != null ? `@${t.step}` : ''}:
            </span>
            <span>{t.message}</span>
          </div>
        ))}
      </div>
    </ScrollArea>
  )
}

function EmbeddedMetrics({ spec }: { spec: EmbeddedSpec }) {
  const allMetricsRaw = useStore(s => s.runs.get(spec.runId)?.loggableMetrics)
  const allMetrics = allMetricsRaw ?? EMPTY_METRICS_MAP
  const runColor = useStore(s => s.runColors.get(spec.runId)) ?? DEFAULT_RUN_COLOR
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const filterNodeId = resolveNodeRef(spec.nodeRef, graph?.nodes)

  const items = useMemo(() => {
    const out: { loggableId: string; name: string; series: typeof allMetrics[string][string] }[] = []
    for (const [lid, byName] of Object.entries(allMetrics)) {
      if (filterNodeId && lid !== filterNodeId) continue
      for (const [name, series] of Object.entries(byName)) {
        if (spec.name && name !== spec.name) continue
        out.push({ loggableId: lid, name, series })
      }
    }
    return out
  }, [allMetrics, filterNodeId, spec.name])

  return (
    <ScrollArea className="h-screen">
      <div className="p-3 space-y-4">
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">No metrics</p>
        ) : (
          items.map(({ loggableId, name, series }) => (
            <MetricBlock key={`${loggableId}:${name}`} name={name} series={series} color={runColor} runId={spec.runId} loggableId={loggableId} />
          ))
        )}
      </div>
    </ScrollArea>
  )
}

function EmbeddedImages({ spec }: { spec: EmbeddedSpec }) {
  const allImagesRaw = useStore(s => s.runs.get(spec.runId)?.loggableImages)
  const allImages = allImagesRaw ?? EMPTY_IMAGES_MAP
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const filterNodeId = resolveNodeRef(spec.nodeRef, graph?.nodes)

  const { items, total } = useMemo(() => {
    const out: { loggableId: string; img: typeof allImages[string][number] }[] = []
    for (const [lid, list] of Object.entries(allImages)) {
      if (filterNodeId && lid !== filterNodeId) continue
      for (const img of list) {
        if (spec.name && img.name !== spec.name) continue
        out.push({ loggableId: lid, img })
      }
    }
    // A repeated name is a time series (one emission per step) — cap the
    // embed at the most recent entries so a 10k-step run doesn't mount
    // thousands of <img> elements in an iframe.
    return { items: out.slice(-MAX_EMBED_MEDIA_ITEMS), total: out.length }
  }, [allImages, filterNodeId, spec.name])

  return (
    <ScrollArea className="h-screen">
      <div className="p-3 space-y-3">
        {total > items.length && (
          <p className="text-xs text-muted-foreground">
            Showing the latest {items.length} of {total} images
          </p>
        )}
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">No images</p>
        ) : (
          items.map(({ loggableId, img }, i) => (
            <ImageItem key={mediaEntryKey(img, i)} runId={spec.runId} loggableId={loggableId} img={img} showTimestamp />
          ))
        )}
      </div>
    </ScrollArea>
  )
}

// Unlike the media galleries, a scene is ONE live viewport regardless of
// how many frames were logged — frames are steps of the same card, not
// separate items — so there is nothing to cap here.
function EmbeddedActions({ spec }: { spec: EmbeddedSpec }) {
  const allActions = useStore(s => s.runs.get(spec.runId)?.loggableActions)
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const filterNodeId = resolveNodeRef(spec.nodeRef, graph?.nodes)

  const scenes = useMemo(() => {
    const out: { loggableId: string; name: string }[] = []
    const seen = new Set<string>()
    for (const [lid, frames] of Object.entries(allActions ?? {})) {
      if (filterNodeId && lid !== filterNodeId) continue
      for (const frame of frames) {
        const name = frame.name || 'scene'
        if (spec.name && name !== spec.name) continue
        const key = `${lid}:${name}`
        if (seen.has(key)) continue
        seen.add(key)
        out.push({ loggableId: lid, name })
      }
    }
    return out
  }, [allActions, filterNodeId, spec.name])

  if (scenes.length === 0) {
    return (
      <div className="p-3 text-xs text-muted-foreground">No 3D scenes</div>
    )
  }

  // A single-scene embed fills the frame; a gallery scrolls.
  if (scenes.length === 1) {
    return (
      <div className="h-screen p-3">
        <ActionCard
          runId={spec.runId}
          loggableId={scenes[0].loggableId}
          name={scenes[0].name}
          showTimestamp
          fillParent
        />
      </div>
    )
  }

  return (
    <ScrollArea className="h-screen">
      <div className="space-y-3 p-3">
        {scenes.map(({ loggableId, name }) => (
          <ActionCard
            key={`${loggableId}:${name}`}
            runId={spec.runId}
            loggableId={loggableId}
            name={name}
            showTimestamp
          />
        ))}
      </div>
    </ScrollArea>
  )
}

function EmbeddedAudio({ spec }: { spec: EmbeddedSpec }) {
  const allAudioRaw = useStore(s => s.runs.get(spec.runId)?.loggableAudio)
  const allAudio = allAudioRaw ?? EMPTY_AUDIO_MAP
  const graph = useStore(s => s.runs.get(spec.runId)?.graph)
  const filterNodeId = resolveNodeRef(spec.nodeRef, graph?.nodes)

  const { items, total } = useMemo(() => {
    const out: { loggableId: string; entry: typeof allAudio[string][number] }[] = []
    for (const [lid, list] of Object.entries(allAudio)) {
      if (filterNodeId && lid !== filterNodeId) continue
      for (const entry of list) {
        if (spec.name && entry.name !== spec.name) continue
        out.push({ loggableId: lid, entry })
      }
    }
    return { items: out.slice(-MAX_EMBED_MEDIA_ITEMS), total: out.length }
  }, [allAudio, filterNodeId, spec.name])

  return (
    <ScrollArea className="h-screen">
      <div className="p-3 space-y-3">
        {total > items.length && (
          <p className="text-xs text-muted-foreground">
            Showing the latest {items.length} of {total} clips
          </p>
        )}
        {items.length === 0 ? (
          <p className="text-xs text-muted-foreground">No audio</p>
        ) : (
          items.map(({ entry }, i) => (
            <AudioItem key={mediaEntryKey(entry, i)} runId={spec.runId} entry={entry} showTimestamp />
          ))
        )}
        {/* loggableId currently unused in AudioItem, but we capture it so we
            can support tag/labels per loggable later without re-keying. */}
        <span className="hidden">{items.map(i => i.loggableId).join(',')}</span>
      </div>
    </ScrollArea>
  )
}

// Module-level empty fallbacks: shared references prevent zustand selectors
// from returning a fresh `[]` / `{}` on every render and re-firing the
// subscription.
const EMPTY_TEXTS: import('@/lib/api').TextEntry[] = []
const EMPTY_METRICS_MAP: Record<string, Record<string, import('@/lib/api').LoggableMetricSeries>> = {}
const EMPTY_IMAGES_MAP: Record<string, import('@/store').ImageEntry[]> = {}
const EMPTY_AUDIO_MAP: Record<string, import('@/store').AudioEntry[]> = {}

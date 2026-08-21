import { Suspense, lazy, useMemo, useState } from 'react'
import { useStore } from '@/store'
import { assignColor } from '@/lib/colors'
import { formatTimestamp } from '@/lib/utils'
import { HeaderActions } from '@/components/node-tabs/HeaderActions'
import { Modal } from '@/components/ui/modal'
import { buildEmbeddedUrl } from '@/hooks/useEmbeddedView'
import { pickFrame, sceneFrames, useActionFrame } from '@/hooks/useActionFrame'
import { buildInstances, type SceneSource } from './sceneSources'

// The 3D stack loads only when a scene is actually rendered. Every import
// of SceneViewer must stay lazy or `three` lands in the main bundle.
const SceneViewer = lazy(() => import('./SceneViewer'))

const EMPTY_RUN_IDS: string[] = []

function ViewerFallback({ label }: { label: string }) {
  return (
    <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
      {label}
    </div>
  )
}

/**
 * One SceneSource per run, for a scene rendered from several runs at once.
 *
 * Comparison views merge every run into ONE scene (one WebGL context, robots
 * placed side by side) rather than a grid of viewers, so this resolves each
 * run's current frame in a single memo instead of per-run hooks.
 */
function useMergedSources(
  runIds: string[], loggableId: string, name: string,
): SceneSource[] {
  const runs = useStore(s => s.runs)
  const runColors = useStore(s => s.runColors)
  const mode = useStore(s => s.timeline.mode)
  const step = useStore(s => s.timeline.step)
  const time = useStore(s => s.timeline.time)

  return useMemo(() => runIds.map((rid, i) => {
    const run = runs.get(rid)
    const frames = sceneFrames(run?.loggableActions[loggableId], name)
    return {
      runId: rid,
      frame: pickFrame(frames, mode, step, time),
      bodyModels: run?.bodyModels ?? {},
      prefix: run?.summary.run_name || rid.slice(0, 8),
      // Run identity owns the palette in comparison views.
      runColor: runColors.get(rid) ?? assignColor(i),
    }
  }), [runIds, runs, runColors, loggableId, name, mode, step, time])
}

interface ActionCardProps {
  runId: string
  loggableId: string
  name: string
  /** Extra runs merged into this scene (comparison views). */
  comparisonRunIds?: string[]
  showTimestamp?: boolean
  fillParent?: boolean
}

export function ActionCard({
  runId, loggableId, name, comparisonRunIds, showTimestamp, fillParent,
}: ActionCardProps) {
  const isComparison = (comparisonRunIds?.length ?? 0) > 0
  const frame = useActionFrame(runId, loggableId, name)
  const bodyModels = useStore(s => s.runs.get(runId)?.bodyModels)
  const settings = useStore(s => s.settings)
  const [hidden, setHidden] = useState<Set<string>>(() => new Set())
  const [expanded, setExpanded] = useState(false)

  const merged = useMergedSources(
    // Hooks cannot be conditional; an empty list makes this a no-op memo.
    isComparison ? comparisonRunIds! : EMPTY_RUN_IDS, loggableId, name,
  )
  const single = useMemo<SceneSource[]>(
    () => [{ runId, frame, bodyModels: bodyModels ?? {} }],
    [runId, frame, bodyModels],
  )
  const sources = isComparison ? merged : single

  const instances = useMemo(
    () => buildInstances(sources, settings.tintInstances, hidden),
    [sources, settings.tintInstances, hidden],
  )
  // Header step/time comes from whichever source is actually driving the
  // card — the anchor run in a merged comparison scene.
  const headline = sources[0]?.frame ?? null
  // Legend entries mirror the instance keys the viewer was given, so a
  // merged comparison scene lists "<run>·<instance>" rows.
  const labels = useMemo(() => instances.map(i => i.key), [instances])
  const labelColor = useMemo(() => {
    const map = new Map<string, string>()
    for (const i of instances) if (i.tint) map.set(i.key, i.tint)
    return map
  }, [instances])

  const toggle = (label: string) => setHidden(prev => {
    const next = new Set(prev)
    if (next.has(label)) next.delete(label)
    else next.add(label)
    return next
  })

  const viewer = (
    <Suspense fallback={<ViewerFallback label="Loading 3D…" />}>
      <SceneViewer
        instances={instances}
        bodyOpacity={settings.bodyOpacity}
        showCollision={settings.showCollision}
        collisionOpacity={settings.collisionOpacity}
        offsetX={settings.modelOffsetX}
        offsetY={settings.modelOffsetY}
        className="h-full w-full"
      />
    </Suspense>
  )

  return (
    <div data-export-atom="action" className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className={`${showTimestamp ? 'text-xs' : 'text-[10px]'} font-medium truncate`}>{name}</span>
          <span className="text-[10px] text-muted-foreground shrink-0">
            {headline?.step != null && `step ${headline.step}`}
            {showTimestamp && headline?.step != null && ' · '}
            {showTimestamp && headline && formatTimestamp(headline.timestamp)}
          </span>
        </div>
        <HeaderActions
          onExpand={() => setExpanded(true)}
          iframeUrl={buildEmbeddedUrl({ runId, node: loggableId, action: name })}
        />
      </div>

      <div
        className={`relative overflow-hidden rounded border border-border bg-muted/20 ${
          fillParent ? 'h-full min-h-[180px]' : 'h-56'
        }`}
      >
        {viewer}
        {instances.length === 0 && (
          <div className="pointer-events-none absolute inset-0">
            <ViewerFallback label={headline ? 'Loading model…' : 'No frames yet'} />
          </div>
        )}
      </div>

      {labels.length > 1 && (
        <div className="flex flex-wrap gap-1">
          {labels.map((label) => {
            const color = labelColor.get(label)
            const off = hidden.has(label)
            return (
              <button
                key={label}
                onClick={() => toggle(label)}
                className={`flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] transition-opacity ${
                  off ? 'opacity-40' : ''
                }`}
                title={off ? `Show ${label}` : `Hide ${label}`}
              >
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: color ?? 'currentColor' }}
                />
                {label}
              </button>
            )
          })}
        </div>
      )}

      {expanded && (
        <Modal open={expanded} onClose={() => setExpanded(false)} title={name} widthClass="max-w-6xl">
          <div className="h-[70vh] w-full overflow-hidden rounded border border-border bg-muted/20">
            {viewer}
          </div>
        </Modal>
      )}
    </div>
  )
}

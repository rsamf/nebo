// Renders one loggable's 3D scenes; works for node- and global-kind loggables.
import { useStore } from '@/store'
import { useActionScenes } from '@/hooks/useActionFrame'
import { ActionCard } from './ActionCard'

interface NodeActionsProps {
  runId: string
  loggableId: string
  // Comparison views merge every run into ONE scene rather than a grid of
  // viewers — see ActionCard's useMergedSources.
  comparisonRunIds?: string[]
  fillParent?: boolean
}

export function NodeActions({
  runId, loggableId, comparisonRunIds, fillParent,
}: NodeActionsProps) {
  const ownScenes = useActionScenes(runId, loggableId)
  const runs = useStore(s => s.runs)

  // In comparison mode the scene list is the union across runs: a scene
  // only one run logged still deserves a card.
  const scenes = comparisonRunIds?.length
    ? [...new Set(comparisonRunIds.flatMap(rid => {
        const frames = runs.get(rid)?.loggableActions[loggableId] ?? []
        return frames.map(f => f.name || 'scene')
      }))]
    : ownScenes

  if (scenes.length === 0) {
    return <div className="text-xs text-muted-foreground">No scenes logged.</div>
  }

  return (
    <div className={fillParent ? 'h-full space-y-3 overflow-auto' : 'space-y-3'}>
      {scenes.map(name => (
        <ActionCard
          key={name}
          runId={runId}
          loggableId={loggableId}
          name={name}
          comparisonRunIds={comparisonRunIds}
          showTimestamp
        />
      ))}
    </div>
  )
}

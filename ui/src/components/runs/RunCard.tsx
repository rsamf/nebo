import { useEffect, useMemo } from 'react'
import { cn } from '@/lib/utils'
import { useContextMenu } from '@/hooks/useContextMenu'
import { useStore } from '@/store'
import { RunContextMenu } from './RunContextMenu'
import { RunHoverInfo } from './RunHoverInfo'
import { runDisplayName } from '@/lib/runTree'
import type { RunSummary } from '@/lib/api'

interface RunCardProps {
  run: RunSummary
  selected: boolean
  onClick: () => void
  /** Tree depth; indents the row to match sibling groups in RunTree. */
  depth?: number
}

export function RunCard({ run, selected, onClick, depth = 0 }: RunCardProps) {
  const getOrAssignRunColor = useStore(s => s.getOrAssignRunColor)
  const runColor = useStore(s => s.runColors.get(run.id))
  const isSelectedForCompare = useStore(s => s.selectedForCompare.has(run.id))
  const selectedRunId = useStore(s => s.selectedRunId)
  const comparisonGroups = useStore(s => s.comparisonGroups)

  // When viewing a comparison group, check if a comparison is active and whether this run is in it
  const comparisonActive = selectedRunId?.startsWith('cmp:') ?? false
  const isInActiveComparison = useMemo(() => {
    if (!comparisonActive || !selectedRunId) return false
    const group = comparisonGroups.get(selectedRunId)
    return group?.runIds.includes(run.id) ?? false
  }, [comparisonActive, selectedRunId, comparisonGroups, run.id])
  const contextMenu = useContextMenu()

  const displayName = runDisplayName(run)

  useEffect(() => {
    getOrAssignRunColor(run.id)
  }, [run.id, getOrAssignRunColor])

  return (
    <RunHoverInfo runId={run.id} side="right">
      {/* div with button semantics instead of <button>: the row hosts the
          context menu as an interactive child, and buttons can't nest. */}
      <div
        role="button"
        tabIndex={0}
        onClick={onClick}
        onKeyDown={e => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onClick()
          }
        }}
        {...contextMenu.handlers}
        className={cn(
          'flex w-full items-center gap-1 rounded px-1.5 py-1 text-left text-xs',
          'transition-colors cursor-pointer hover:bg-muted/50',
          selected && 'bg-muted',
          isSelectedForCompare && 'ring-2 ring-primary/40',
          comparisonActive && !isInActiveComparison && 'opacity-40',
        )}
        style={{ paddingLeft: 6 + depth * 12 }}
      >
        {/* The dot sits in a chevron-sized slot so run labels line up with
            group labels at the same depth (groups lead with a h-3.5 chevron). */}
        <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: runColor ?? 'transparent' }}
          />
        </span>
        <span className="min-w-0 flex-1 truncate">{displayName}</span>
        {isSelectedForCompare && (
          <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-primary" title="Selected for compare" />
        )}
        <RunContextMenu
          runId={run.id}
          isOpen={contextMenu.isOpen}
          position={contextMenu.position}
          onClose={contextMenu.close}
        />
      </div>
    </RunHoverInfo>
  )
}

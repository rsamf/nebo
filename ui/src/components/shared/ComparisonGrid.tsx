import type { ReactNode } from 'react'
import { useStore } from '@/store'
import { getGridDimensions } from '@/lib/grid'
import { cn } from '@/lib/utils'

interface ComparisonGridProps {
  runIds: string[]
  children: (runId: string, index: number) => ReactNode
  // When true, the grid fills its parent's height and distributes
  // rows evenly via `grid-template-rows: 1fr`. Each cell's content
  // area becomes a bare flex-1 box (no overflow-auto) so child tab
  // implementations can own their own scroll, matching the
  // single-run fillParent pattern.
  fillParent?: boolean
}

export function ComparisonGrid({ runIds, children, fillParent }: ComparisonGridProps) {
  const runColors = useStore(s => s.runColors)
  const runs = useStore(s => s.runs)

  // No runs to compare → render nothing (rather than the legacy
  // 1×1 "ghost cell" that getGridDimensions(0) would produce).
  if (runIds.length === 0) return null

  const { cols, rows } = getGridDimensions(runIds.length)
  const totalCells = cols * rows

  return (
    <div
      className={cn('grid gap-px bg-border', fillParent && 'h-full')}
      style={{
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        ...(fillParent ? { gridTemplateRows: `repeat(${rows}, 1fr)` } : {}),
      }}
    >
      {Array.from({ length: totalCells }, (_, i) => {
        const runId = runIds[i]
        if (!runId) {
          return <div key={`empty-${i}`} className="bg-background" />
        }

        const color = runColors.get(runId) ?? '#60a5fa'
        const run = runs.get(runId)
        const displayName =
          run?.summary.run_name ||
          run?.summary.script_path.split('/').pop() ||
          runId

        return (
          <div
            key={runId}
            className={cn(
              'bg-background min-w-0 flex flex-col',
              // CSS Grid items default to `min-height: auto`, so when a
              // cell's content is taller than its 1fr row the cell
              // expands to fit and pushes the whole grid past the
              // `h-full` boundary. `min-h-0` lets the cell honor its
              // row size; `overflow-hidden` is the belt-and-suspenders
              // clip for any child that ignores the bound. Only matters
              // in fillParent mode (where rows are 1fr) — outside it
              // the grid takes the natural height of its tallest cell.
              fillParent && 'min-h-0 overflow-hidden',
            )}
          >
            <div
              className="flex items-center gap-1.5 px-2 py-1 border-b border-border shrink-0"
              style={{ borderTop: `3px solid ${color}` }}
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: color }}
              />
              <span className="text-[10px] text-muted-foreground truncate">
                {displayName}
              </span>
            </div>
            <div
              className={cn(
                'flex-1 min-h-0',
                // When the grid fills its parent each child can own its
                // own scroll container (e.g. VirtualizedImageList). Outside
                // fillParent we keep the legacy cell-level overflow.
                !fillParent && 'overflow-auto',
              )}
            >
              {children(runId, i)}
            </div>
          </div>
        )
      })}
    </div>
  )
}

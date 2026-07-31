import { useStore } from '@/store'
import { RunCard } from './RunCard'
import { cn } from '@/lib/utils'
import { ChevronRight, FileText } from 'lucide-react'
import {
  byStartedDesc,
  childGroupsOf,
  filterRunTree,
  membersOf,
  runDisplayName,
  type TreeFilter,
} from '@/lib/runTree'
import type { RunSummary } from '@/lib/api'

/** The sidebar's group hierarchy: collapsible groups with their member runs,
 *  plus root (ungrouped) runs. Read-only — reorganization is via the CLI/MCP. */
export function RunTree({ query = '' }: { query?: string }) {
  const runs = useStore(s => s.runs)
  const runTree = useStore(s => s.runTree)
  const selectedRunId = useStore(s => s.selectedRunId)
  const selectRun = useStore(s => s.selectRun)

  const summaries = Array.from(runs.values(), r => r.summary)
  const byId = new Map(summaries.map(s => [s.id, s] as [string, RunSummary]))
  const placements = runTree.runs

  const filter = filterRunTree(runTree, byId, s => runDisplayName(s), query)
  const searching = filter !== null

  const topGroups = Object.keys(runTree.groups)
    .filter(g => !g.includes('/'))
    .filter(g => !filter || filter.groups.has(g))
    .sort()
  const rootRuns = summaries
    .filter(s => !(s.id in placements))
    .filter(s => !filter || filter.runs.has(s.id))
    .sort(byStartedDesc)

  if (searching && topGroups.length === 0 && rootRuns.length === 0) {
    return <div className="px-1.5 py-2 text-xs text-muted-foreground">No matching runs</div>
  }

  return (
    <>
      {topGroups.map(g => (
        <GroupBranch key={g} path={g} depth={0} byId={byId} filter={filter} searching={searching} />
      ))}
      {rootRuns.map(s => (
        <RunCard
          key={s.id}
          run={s}
          depth={0}
          selected={s.id === selectedRunId}
          onClick={() => selectRun(s.id)}
        />
      ))}
    </>
  )
}

function GroupBranch({
  path,
  depth,
  byId,
  filter,
  searching,
}: {
  path: string
  depth: number
  byId: Map<string, RunSummary>
  filter: TreeFilter | null
  searching: boolean
}) {
  const runTree = useStore(s => s.runTree)
  const userExpanded = useStore(s => s.expandedGroups.has(path))
  const toggle = useStore(s => s.toggleGroupExpanded)
  const selectDoc = useStore(s => s.selectDoc)
  const selectedDoc = useStore(s => s.selectedDoc)
  const selectRun = useStore(s => s.selectRun)
  const selectedRunId = useStore(s => s.selectedRunId)

  // While searching, force every surviving branch open so matches aren't
  // hidden inside collapsed folders — derived, not written to the store, so
  // clearing the query restores exactly what the user had open.
  const expanded = searching || userExpanded

  const leaf = path.split('/').pop() ?? path
  const docs = runTree.groups[path]?.docs ?? []
  const childGroups = childGroupsOf(runTree.groups, path).filter(
    g => !filter || filter.groups.has(g),
  )
  const memberRuns = membersOf(runTree.runs, path, byId).filter(
    s => !filter || filter.runs.has(s.id),
  )

  return (
    <div>
      {/* Clicking a group anywhere only toggles collapse — there is no
          group view to navigate to. Its markdown docs are tree leaves. */}
      <button
        onClick={() => toggle(path)}
        className="flex w-full items-center gap-1 rounded px-1.5 py-1 text-left text-xs hover:bg-muted/50"
        style={{ paddingLeft: 6 + depth * 12 }}
        title={path}
      >
        <ChevronRight
          className={cn('h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform', expanded && 'rotate-90')}
        />
        <span className="truncate font-medium">{leaf}</span>
      </button>
      {expanded && (
        <div>
          {docs.map(name => {
            const isSel = selectedDoc?.group === path && selectedDoc?.name === name
            return (
              <button
                key={name}
                onClick={() => selectDoc({ group: path, name })}
                className={cn(
                  'flex w-full items-center gap-1 rounded px-1.5 py-1 text-left text-xs',
                  isSel ? 'bg-muted' : 'text-muted-foreground hover:bg-muted/50 hover:text-foreground',
                )}
                style={{ paddingLeft: 6 + (depth + 1) * 12 }}
                title={`${path}/${name}`}
              >
                {/* chevron-sized slot keeps doc labels aligned with runs */}
                <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center">
                  <FileText className="h-3 w-3" />
                </span>
                <span className="min-w-0 flex-1 truncate">{name}</span>
              </button>
            )
          })}
          {childGroups.map(g => (
            <GroupBranch
              key={g}
              path={g}
              depth={depth + 1}
              byId={byId}
              filter={filter}
              searching={searching}
            />
          ))}
          {memberRuns.map(s => (
            <RunCard
              key={s.id}
              run={s}
              depth={depth + 1}
              selected={s.id === selectedRunId}
              onClick={() => selectRun(s.id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

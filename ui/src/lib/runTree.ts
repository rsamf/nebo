import type { RunSummary, TreeData } from './api'

/** Newest-first by start time; runs with no started_at sort last. */
export function byStartedDesc(a: RunSummary, b: RunSummary): number {
  const at = a.started_at ? new Date(a.started_at).getTime() : 0
  const bt = b.started_at ? new Date(b.started_at).getTime() : 0
  return bt - at
}

/** Index run summaries by id — the shape `membersOf` consumes. */
export function summariesById(
  runs: Map<string, { summary: RunSummary }>,
): Map<string, RunSummary> {
  return new Map(Array.from(runs.values(), r => [r.summary.id, r.summary]))
}

/** The runs placed directly in `path` (not in its subgroups), newest first.
 *  Placements naming runs the store hasn't loaded are skipped. */
export function membersOf(
  placements: TreeData['runs'],
  path: string,
  byId: Map<string, RunSummary>,
): RunSummary[] {
  return Object.entries(placements)
    .filter(([, g]) => g === path)
    .map(([id]) => byId.get(id))
    .filter((s): s is RunSummary => Boolean(s))
    .sort(byStartedDesc)
}

/** The immediate subgroups of `path` — group paths are flat strings, so
 *  nesting is a prefix match with no further '/' in the remainder. */
export function childGroupsOf(groups: TreeData['groups'], path: string): string[] {
  const prefix = path + '/'
  return Object.keys(groups)
    .filter(g => g.startsWith(prefix) && !g.slice(prefix.length).includes('/'))
    .sort()
}

/** A run's label: the user's rename wins, then the run's own name, then the
 *  script's basename. */
export function runDisplayName(run: RunSummary, customName?: string): string {
  return customName || run.run_name || (run.script_path.split('/').pop() ?? run.script_path)
}

/** What a search query leaves visible. `null` means "no query" — render all. */
export interface TreeFilter {
  groups: Set<string>
  runs: Set<string>
}

/**
 * Filter the tree by a search query, keeping the ancestors of every match so
 * matches stay reachable (same shape as lib/streams.ts:flattenRows).
 *
 * A run matches on its display name or its run id. A group matches on its
 * path, and a matched group keeps its whole subtree — searching "baseline"
 * should show that folder with its runs in it, not an empty folder.
 */
export function filterRunTree(
  tree: TreeData,
  byId: Map<string, RunSummary>,
  labelOf: (run: RunSummary) => string,
  query: string,
): TreeFilter | null {
  const q = query.trim().toLowerCase()
  if (!q) return null

  const allGroups = Object.keys(tree.groups)
  const matchedGroups = allGroups.filter(g => g.toLowerCase().includes(q))
  const underMatchedGroup = (path: string) =>
    matchedGroups.some(mg => path === mg || path.startsWith(mg + '/'))

  const runs = new Set<string>()
  for (const [id, summary] of byId) {
    const placement = tree.runs[id]
    const hit =
      labelOf(summary).toLowerCase().includes(q) ||
      id.toLowerCase().includes(q) ||
      (placement != null && underMatchedGroup(placement))
    if (hit) runs.add(id)
  }

  const groups = new Set<string>()
  const addWithAncestors = (path: string) => {
    const parts = path.split('/')
    for (let i = 1; i <= parts.length; i++) groups.add(parts.slice(0, i).join('/'))
  }
  for (const g of matchedGroups) {
    addWithAncestors(g)
    for (const other of allGroups) if (other.startsWith(g + '/')) groups.add(other)
  }
  for (const id of runs) {
    const placement = tree.runs[id]
    if (placement) addWithAncestors(placement)
  }
  return { groups, runs }
}

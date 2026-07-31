import { useMemo } from 'react'
import { useStore } from '@/store'
import { useComparisonContext } from '@/hooks/useComparisonContext'

/**
 * True if this loggable has any content worth surfacing in a tab view —
 * text, metrics, images, or audio. Used to decide whether to
 * render the bordered tab area at all (an empty function node or a
 * globals-only run shouldn't show an empty tab strip).
 */
export function useLoggableHasContent(runId: string, loggableId: string): boolean {
  const run = useStore(s => s.runs.get(runId))
  const runs = useStore(s => s.runs)
  const { isComparison, runIds: comparisonRunIds } = useComparisonContext()

  return useMemo(() => {
    const checkRun = (rid: string): boolean => {
      const r = runs.get(rid)
      if (!r) return false
      // O(1) checks first — the text scan is O(n) over a possibly huge array.
      const m = r.loggableMetrics?.[loggableId]
      if (m && Object.keys(m).length > 0) return true
      if ((r.loggableImages?.[loggableId]?.length ?? 0) > 0) return true
      if ((r.loggableAudio?.[loggableId]?.length ?? 0) > 0) return true
      return r.texts?.some(t => t.node === loggableId) ?? false
    }

    if (isComparison) return comparisonRunIds.some(checkRun)
    if (!run) return false
    return checkRun(runId)
  }, [run, runs, runId, loggableId, isComparison, comparisonRunIds])
}

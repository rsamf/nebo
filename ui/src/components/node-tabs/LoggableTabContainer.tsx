import { useMemo } from 'react'
import { useStore, type NodeTab } from '@/store'
import { cn } from '@/lib/utils'
import { LoggableTabContent } from './LoggableTabContent'
import { useComparisonContext } from '@/hooks/useComparisonContext'

interface LoggableTabContainerProps {
  runId: string
  loggableId: string
  // When true the container fills its parent (flex-col, h-full) and the
  // tab-content area scrolls internally so the tab strip stays pinned.
  // Used inside DAG nodes which now have a fixed total height. Default
  // (false) keeps the legacy block-layout with a max-h-[420px] cap on
  // the content area, used by EmbeddedView's natural-flow scroll.
  fillParent?: boolean
}

const allTabs: { key: NodeTab; label: string; alwaysShow: boolean }[] = [
  { key: 'text', label: 'Text', alwaysShow: false },
  { key: 'metrics', label: 'Metrics', alwaysShow: false },
  { key: 'images', label: 'Images', alwaysShow: false },
  { key: 'audio', label: 'Audio', alwaysShow: false },
]

const VALID_TABS: readonly NodeTab[] = ['text', 'metrics', 'images', 'audio']

export function LoggableTabContainer({ runId, loggableId, fillParent = false }: LoggableTabContainerProps) {
  const run = useStore(s => s.runs.get(runId))
  const runs = useStore(s => s.runs)
  const userTab = useStore(s => s.selectedTabs.get(runId)?.get(loggableId))
  const setSelectedTab = useStore(s => s.setSelectedTab)
  const { isComparison, runIds: comparisonRunIds } = useComparisonContext()

  // Nodes can declare `ui={"default_tab": "metrics"}` on @nb.fn to open a
  // specific tab when the card is first expanded. User clicks override.
  const defaultTab = useMemo<NodeTab>(() => {
    const hint = run?.graph?.nodes?.[loggableId]?.ui_hints?.default_tab
    return typeof hint === 'string' && (VALID_TABS as readonly string[]).includes(hint)
      ? (hint as NodeTab)
      : 'text'
  }, [run?.graph?.nodes, loggableId])

  // User-selected tab persists across graph re-renders by living in the
  // store (`selectedTabs`). When the user has not yet clicked a tab on
  // this loggable, fall through to `defaultTab` so the SDK-declared
  // `default_tab` still seeds the initial view.
  const activeTab: NodeTab = userTab ?? defaultTab

  // In comparison mode, aggregate data availability across all runs
  const hasText = useMemo(() => {
    if (isComparison) {
      return comparisonRunIds.some(rid => {
        const r = runs.get(rid)
        if (!r) return false
        return r.texts?.some(t => t.node === loggableId) ?? false
      })
    }
    if (!run) return false
    return run.texts?.some(t => t.node === loggableId) ?? false
  }, [isComparison, comparisonRunIds, runs, run, loggableId])

  const hasMetrics = useMemo(() => {
    if (isComparison) {
      return comparisonRunIds.some(rid => {
        const r = runs.get(rid)
        return !!r?.loggableMetrics?.[loggableId] && Object.keys(r.loggableMetrics[loggableId]).length > 0
      })
    }
    return !!run?.loggableMetrics?.[loggableId] && Object.keys(run.loggableMetrics[loggableId]).length > 0
  }, [isComparison, comparisonRunIds, runs, run, loggableId])

  const hasImages = useMemo(() => {
    if (isComparison) {
      return comparisonRunIds.some(rid => (runs.get(rid)?.loggableImages?.[loggableId]?.length ?? 0) > 0)
    }
    return (run?.loggableImages?.[loggableId]?.length ?? 0) > 0
  }, [isComparison, comparisonRunIds, runs, run, loggableId])

  const hasAudio = useMemo(() => {
    if (isComparison) {
      return comparisonRunIds.some(rid => (runs.get(rid)?.loggableAudio?.[loggableId]?.length ?? 0) > 0)
    }
    return (run?.loggableAudio?.[loggableId]?.length ?? 0) > 0
  }, [isComparison, comparisonRunIds, runs, run, loggableId])

  const visibleTabs = useMemo(() => {
    return allTabs.filter(tab => {
      if (tab.alwaysShow) return true
      switch (tab.key) {
        case 'text': return hasText
        case 'metrics': return hasMetrics
        case 'images': return hasImages
        case 'audio': return hasAudio
        default: return false
      }
    })
  }, [hasText, hasMetrics, hasImages, hasAudio])

  // Nothing to show — let parents collapse the surrounding chrome (no
  // empty bordered tab strip on nodes that haven't logged anything).
  if (visibleTabs.length === 0) return null

  // Only reset to the default if the user hasn't explicitly chosen a tab.
  // Once the user clicks a tab, keep it even if it temporarily leaves
  // visibleTabs (e.g., while data is still loading via WebSocket).
  // When the default tab isn't itself visible (e.g. a node has images
  // but no logs), snap to the highest-priority visible tab instead so
  // the card opens on actual content.
  const isActiveVisible = visibleTabs.some(t => t.key === activeTab)
  const fallbackTab =
    visibleTabs.find(t => t.key === defaultTab)?.key ?? visibleTabs[0].key
  const resolvedTab = isActiveVisible
    ? activeTab
    : userTab !== undefined
      ? activeTab
      : fallbackTab

  return (
    <div className={fillParent ? 'flex flex-col h-full min-h-0' : undefined}>
      {/* Tab bar */}
      <div className={cn(
        'flex items-center border-b border-border',
        fillParent && 'shrink-0',
      )}>
        <div className="flex flex-1">
          {visibleTabs.map(tab => (
            <button
              key={tab.key}
              className={cn(
                'px-3 py-1.5 text-xs font-medium transition-colors relative',
                resolvedTab === tab.key
                  ? 'text-foreground border-b-2 border-primary'
                  : 'text-muted-foreground hover:text-foreground',
              )}
              onClick={(e) => {
                e.stopPropagation()
                setSelectedTab(runId, loggableId, tab.key)
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {/* Tab content */}
      {/*
        In fillParent mode every tab body owns its own scroll container
        (NodeText / NodeImages / NodeMetrics / NodeAudio all switch to
        `h-full overflow-auto` when fillParent is set), so we drop the
        wrapper's overflow to avoid nested scrollbars. Default mode
        keeps the legacy max-h cap with overflow on the wrapper.
      */}
      <div
        className={cn(
          'p-3',
          fillParent ? 'flex-1 min-h-0' : 'max-h-[420px] overflow-auto',
        )}
        onClick={e => e.stopPropagation()}
      >
        <LoggableTabContent runId={runId} loggableId={loggableId} tab={resolvedTab} comparisonRunIds={isComparison ? comparisonRunIds : undefined} fillParent={fillParent} />
      </div>
    </div>
  )
}

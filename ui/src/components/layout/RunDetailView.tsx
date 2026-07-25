import { useState } from 'react'
import { useStore } from '@/store'
import { useRunData } from '@/hooks/useRunData'
import { useIsDesktop } from '@/hooks/useMediaQuery'
import { useComparisonContext } from '@/hooks/useComparisonContext'
import { DagGraph } from '@/components/graph/DagGraph'
import { LoggableGridView } from '@/components/graph/LoggableGridView'
import { RunHoverInfo } from '@/components/runs/RunHoverInfo'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { PanelRight } from 'lucide-react'
import { Button } from '@/components/ui/button'

/** Muted monospace run id, click-to-copy; hover shows the run's vitals. */
export function RunIdChip({ runId }: { runId: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <RunHoverInfo runId={runId} side="bottom">
      <button
        onClick={() => {
          navigator.clipboard.writeText(runId).then(() => {
            setCopied(true)
            setTimeout(() => setCopied(false), 1500)
          })
        }}
        className="text-xs font-mono text-muted-foreground hover:text-foreground transition-colors"
      >
        {copied ? 'copied!' : runId}
      </button>
    </RunHoverInfo>
  )
}

export function RunDetailView() {
  const selectedRunId = useStore(s => s.selectedRunId)
  const { isComparison, runIds: comparisonRunIds } = useComparisonContext()

  // For comparison groups, use the first run's data for the graph view
  const effectiveRunId = isComparison ? comparisonRunIds[0] ?? selectedRunId : selectedRunId
  const run = useRunData(effectiveRunId)
  const isDesktop = useIsDesktop()
  const viewMode = useStore(s => s.viewMode)
  const setViewMode = useStore(s => s.setViewMode)
  const effectiveViewMode = viewMode
  const runColors = useStore(s => s.runColors)
  const runNames = useStore(s => s.runNames)
  const runs = useStore(s => s.runs)
  const rightPanelOpen = useStore(s => s.rightPanelOpen)
  const toggleRightPanel = useStore(s => s.toggleRightPanel)

  if (!selectedRunId) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">Select a run to view details</p>
      </div>
    )
  }
  if (!run) {
    // selectedRunId is set (e.g., from a deep-link `?run=<id>` URL
    // param) but the run hasn't been streamed into the store yet.
    // Show a loading state instead of "Select a run", which is
    // misleading when a run IS selected.
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground">
        <p className="text-sm">Loading run…</p>
      </div>
    )
  }

  const scriptName = isComparison
    ? `Comparing ${comparisonRunIds.length} runs`
    : (run.summary.run_name ?? run.summary.script_path.split('/').pop() ?? run.summary.script_path)

  return (
    <div className="flex flex-col h-full">
      {/* Run header (desktop only - mobile has its own MobileRunView) */}
      {isDesktop && (
        <div className="border-b border-border shrink-0">
          <div className="flex items-center gap-3 px-4 py-2">
            {/* Text items align on their baselines (mixed font sizes look
                off-kilter when box-centered); the group as a whole still
                centers against the tabs/buttons. */}
            <div className="flex items-baseline gap-3 min-w-0">
              <span className="text-sm font-medium whitespace-nowrap">{scriptName}</span>
              {!isComparison && <RunIdChip runId={run.summary.id} />}
              {run.graph?.workflow_description && (
                <span className="text-xs text-muted-foreground truncate max-w-xs" title={run.graph.workflow_description}>
                  {run.graph.workflow_description.split('\n')[0].replace(/^#\s*/, '')}
                </span>
              )}
            </div>
            <Tabs
              value={viewMode}
              onValueChange={(v) => setViewMode(v as 'graph' | 'flat')}
              className="ml-auto"
            >
              <TabsList className="h-6">
                <TabsTrigger value="graph" className="text-xs h-5 px-2">DAG</TabsTrigger>
                <TabsTrigger value="flat" className="text-xs h-5 px-2">Flat</TabsTrigger>
              </TabsList>
            </Tabs>
            <Button
              variant="ghost"
              onClick={toggleRightPanel}
              className="px-1.5 py-1 h-auto"
              title={rightPanelOpen ? 'Close settings panel' : 'Open settings panel'}
            >
              <PanelRight className="h-4 w-4 text-muted-foreground" />
            </Button>
          </div>
          {/* Comparison banner */}
          {isComparison && comparisonRunIds.length > 0 && (() => {
            const names = comparisonRunIds.map(rid => {
              const r = runs.get(rid)
              return runNames.get(rid) || r?.summary.run_name || r?.summary.script_path.split('/').pop() || rid
            })
            const fullText = `Showing graph of ${names[0]}, and comparing it with ${names.slice(1).join(', ')}`
            return (
              <div
                className="truncate whitespace-nowrap px-4 py-1.5 border-t border-border bg-muted/30 text-xs"
                title={fullText}
              >
                <span className="text-muted-foreground">Showing graph of </span>
                {comparisonRunIds.map((rid, i) => {
                  const color = runColors.get(rid) ?? '#60a5fa'
                  return (
                    <span key={rid} className="inline-flex items-center gap-1">
                      {i === 1 && <span className="text-muted-foreground">, and comparing it with </span>}
                      {i > 1 && <span className="text-muted-foreground">, </span>}
                      <span className="w-2 h-2 rounded-full inline-block shrink-0" style={{ backgroundColor: color }} />
                      <span className="font-medium text-foreground">{names[i]}</span>
                    </span>
                  )
                })}
              </div>
            )
          })()}
        </div>
      )}

      {/* Main content */}
      <div className="flex-1 overflow-hidden flex">
        {effectiveViewMode === 'flat' ? (
          <div className="flex-1 overflow-hidden">
            <LoggableGridView runId={effectiveRunId!} />
          </div>
        ) : (
          <div className="flex-1 overflow-hidden">
            <DagGraph runId={effectiveRunId!} />
          </div>
        )}
      </div>
    </div>
  )
}

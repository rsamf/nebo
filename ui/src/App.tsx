import { useSyncExternalStore } from 'react'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useIsDesktop } from '@/hooks/useMediaQuery'
import { useStore } from '@/store'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Sidebar } from '@/components/layout/Sidebar'
import { MobileApp } from '@/components/mobile/MobileApp'
import { RunDetailView } from '@/components/layout/RunDetailView'
import { GroupPage } from '@/components/layout/GroupPage'
import { RightPanel } from '@/components/layout/RightPanel'
import { Notice } from '@/components/shared/Notice'
import { TooltipProvider } from '@/components/ui/tooltip'
import { useEmbeddedView } from '@/hooks/useEmbeddedView'
import { EmbeddedView } from '@/components/embedded/EmbeddedView'
import { ChartTooltip } from '@/components/charts/ChartTooltip'
import { TokenPrompt } from '@/components/auth/TokenPrompt'
import { getUnauthorized, subscribeUnauthorized } from '@/lib/auth'
import { Tracker } from '@/components/timeline/Tracker'

export default function App() {
  useWebSocket()
  const isDesktop = useIsDesktop()
  const selectedRunId = useStore(s => s.selectedRunId)
  const selectedGroup = useStore(s => s.selectedGroup)
  const reconnecting = useStore(s => s.reconnecting)
  const connected = useStore(s => s.connected)
  const rightPanelOpen = useStore(s => s.rightPanelOpen)
  const embedded = useEmbeddedView()
  const unauthorized = useSyncExternalStore(subscribeUnauthorized, getUnauthorized, getUnauthorized)

  // 401 from any HTTP fetch flips the unauthorized flag. We block both
  // the embedded slice view and the full dashboard behind the token
  // prompt — without it, the WS reconnect loop just shows a misleading
  // "Reconnecting…" forever, since the daemon refuses the handshake.
  if (unauthorized) {
    return (
      <TooltipProvider delayDuration={200}>
        <ErrorBoundary label="TokenPrompt">
          <TokenPrompt />
        </ErrorBoundary>
      </TooltipProvider>
    )
  }

  if (embedded) {
    return (
      <TooltipProvider delayDuration={200}>
        <ErrorBoundary label="EmbeddedView">
          <EmbeddedView spec={embedded} />
        </ErrorBoundary>
        <ChartTooltip />
      </TooltipProvider>
    )
  }

  return (
    <TooltipProvider delayDuration={200}>
    <div className="h-screen flex flex-col overflow-hidden">
      {/* Reconnection banner */}
      {!connected && reconnecting && (
        <div className="bg-yellow-500/10 border-b border-yellow-500/30 px-4 py-1.5 text-xs text-yellow-500 text-center shrink-0">
          Reconnecting to daemon...
        </div>
      )}
      {!connected && !reconnecting && (
        <div className="bg-muted border-b border-border px-4 py-1.5 text-xs text-muted-foreground text-center shrink-0">
          Not connected to daemon. Start one with <code className="bg-background px-1 py-0.5 rounded">nebo serve</code>
        </div>
      )}

      {isDesktop ? (
        /* Desktop: (sidebar + detail + right panel) above, full-width tracker below */
        <div className="flex flex-1 flex-col overflow-hidden">
          <div className="flex flex-1 overflow-hidden">
            <div className="w-64 shrink-0 overflow-hidden">
              <ErrorBoundary label="Sidebar"><Sidebar /></ErrorBoundary>
            </div>
            <div className="flex-1 overflow-hidden">
              {selectedGroup ? (
                <ErrorBoundary label="GroupPage"><GroupPage path={selectedGroup} /></ErrorBoundary>
              ) : (
                <ErrorBoundary label="RunDetailView"><RunDetailView /></ErrorBoundary>
              )}
            </div>
            {selectedRunId && rightPanelOpen && (
              <div className="w-80 shrink-0 overflow-hidden">
                <ErrorBoundary label="RightPanel"><RightPanel runId={selectedRunId} /></ErrorBoundary>
              </div>
            )}
          </div>
          {selectedRunId && (
            <ErrorBoundary label="Tracker"><Tracker runId={selectedRunId} /></ErrorBoundary>
          )}
        </div>
      ) : (
        /* Mobile: the dedicated touch experience (run list → run view
           with DAG ⇄ Feed toggle, heat-strip tracker, bottom sheets). */
        <div className="flex-1 overflow-hidden flex flex-col">
          <MobileApp />
        </div>
      )}
    </div>
    <ChartTooltip />
    <Notice />
    </TooltipProvider>
  )
}

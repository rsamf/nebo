import { useEffect, useState } from 'react'
import { useStore } from '@/store'
import { useRunData } from '@/hooks/useRunData'
import { runDisplayName } from '@/lib/runTree'
import { MobileDagCanvas } from './MobileDagCanvas'
import { MobileFeed } from './MobileFeed'
import { MobileTracker } from './MobileTracker'
import { MobileNodeSheet } from './MobileNodeSheet'
import { MobileRunInfoSheet } from './MobileRunInfoSheet'
import { MobileAlertsSheet } from './MobileAlertsSheet'
import { MobileSettingsSheet } from './MobileSettingsSheet'
import { shortId } from './util'
import { MOBILE_ICON_BUTTON_CLASS } from './primitives'
import { ArrowLeft, Bell, Settings } from 'lucide-react'

// One run, full-screen: header (back / title → info sheet / bell /
// gear), DAG or Feed body (toggled from the tracker bar), persistent
// tracker at the bottom, and the overlay sheets.
export function MobileRunView({ runId }: { runId: string }) {
  const run = useRunData(runId)
  const selectRun = useStore(s => s.selectRun)
  const selectGroup = useStore(s => s.selectGroup)
  const runTree = useStore(s => s.runTree)
  const viewMode = useStore(s => s.viewMode)

  const [nodeSheet, setNodeSheet] = useState<string | null>(null)
  const [infoOpen, setInfoOpen] = useState(false)
  const [alertsOpen, setAlertsOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  // A nebo:// link can be followed from inside a sheet (the notes markdown
  // in the info sheet). Close every overlay so the feed can scroll the
  // target card into view behind them. Deferred to a frame so the effect
  // body never sets state synchronously.
  const pendingNavTarget = useStore(s => s.pendingNavTarget)
  useEffect(() => {
    if (!pendingNavTarget) return
    const raf = requestAnimationFrame(() => {
      setInfoOpen(false)
      setAlertsOpen(false)
      setSettingsOpen(false)
      setNodeSheet(null)
    })
    return () => cancelAnimationFrame(raf)
  }, [pendingNavTarget])

  if (!run) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Loading run…
      </div>
    )
  }

  const name = runDisplayName(run.summary)
  const group = runTree.runs[runId]

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-border">
        <div className="flex items-center gap-2.5 px-3 pb-2.5 pt-3">
          {/* Back mirrors the hierarchy: a grouped run returns to its
              parent group's page, a root run to the run list. */}
          <button
            onClick={() => (group ? selectGroup(group) : selectRun(null))}
            aria-label={group ? `Back to ${group}` : 'Back to runs'}
            className={`${MOBILE_ICON_BUTTON_CLASS} bg-muted`}
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <button
            onClick={() => setInfoOpen(true)}
            className="min-w-0 flex-1 text-left"
          >
            {group && (
              <div className="truncate text-[11px] font-medium text-muted-foreground">{group}</div>
            )}
            <div className="truncate text-[15px] font-semibold leading-tight">{name}</div>
            <div className="truncate font-mono text-[11px] text-muted-foreground">
              {shortId(run.summary.id)}
            </div>
          </button>
          <button
            onClick={() => setAlertsOpen(true)}
            aria-label="Alerts"
            className={`${MOBILE_ICON_BUTTON_CLASS} text-muted-foreground`}
          >
            <Bell className="h-[18px] w-[18px]" />
          </button>
          <button
            onClick={() => setSettingsOpen(true)}
            aria-label="View settings"
            className={`${MOBILE_ICON_BUTTON_CLASS} text-muted-foreground`}
          >
            <Settings className="h-[18px] w-[18px]" />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        {viewMode === 'graph' ? (
          <MobileDagCanvas runId={runId} onNodeTap={setNodeSheet} />
        ) : (
          <MobileFeed runId={runId} />
        )}
      </div>

      <MobileTracker runId={runId} />

      <MobileNodeSheet runId={runId} loggableId={nodeSheet} onClose={() => setNodeSheet(null)} />
      {infoOpen && <MobileRunInfoSheet runId={runId} onClose={() => setInfoOpen(false)} />}
      {alertsOpen && (
        <MobileAlertsSheet
          runId={runId}
          onClose={() => setAlertsOpen(false)}
          onOpenNode={setNodeSheet}
        />
      )}
      {settingsOpen && <MobileSettingsSheet runId={runId} onClose={() => setSettingsOpen(false)} />}
    </div>
  )
}

import { useStore } from '@/store'
import { DEFAULT_RUN_COLOR } from '@/lib/colors'
import { useLoggableHasContent } from '@/hooks/useLoggableHasContent'
import { LoggableTabContainer } from '@/components/node-tabs/LoggableTabContainer'
import { MobileSheet } from './MobileSheet'
import { loggableDisplayName } from './util'

// Bottom sheet for one loggable: the full desktop tab set (Text /
// Metrics / Images / Audio) inside a fixed-height sheet.
export function MobileNodeSheet({
  runId,
  loggableId,
  onClose,
}: {
  runId: string
  loggableId: string | null
  onClose: () => void
}) {
  if (!loggableId) return null
  return <NodeSheet runId={runId} loggableId={loggableId} onClose={onClose} />
}

function NodeSheet({
  runId,
  loggableId,
  onClose,
}: {
  runId: string
  loggableId: string
  onClose: () => void
}) {
  const run = useStore(s => s.runs.get(runId))
  const runColor = useStore(s => s.runColors.get(runId)) ?? DEFAULT_RUN_COLOR
  const hasContent = useLoggableHasContent(runId, loggableId)

  const node = run?.graph?.nodes[loggableId]
  const name = loggableDisplayName(run, loggableId)
  const meta = node
    ? `${node.func_name || loggableId} · ×${node.exec_count}`
    : loggableId

  return (
    <MobileSheet onClose={onClose} heightClass="h-[68vh]">
      <div className="flex shrink-0 items-center gap-2 px-4 pb-2.5">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: runColor }} />
        <span className="min-w-0 flex-1 truncate text-base font-semibold">{name}</span>
        <span className="shrink-0 font-mono text-[11px] text-muted-foreground">{meta}</span>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden px-2">
        {hasContent ? (
          <LoggableTabContainer runId={runId} loggableId={loggableId} fillParent />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            Nothing logged on this node yet
          </div>
        )}
      </div>
    </MobileSheet>
  )
}

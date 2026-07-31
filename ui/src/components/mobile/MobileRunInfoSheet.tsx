import { useState } from 'react'
import { useStore } from '@/store'
import { runDisplayName } from '@/lib/runTree'
import { formatConfigValue } from '@/lib/utils'
import { NeboMarkdown } from '@/components/shared/NeboMarkdown'
import { Modal } from '@/components/ui/modal'
import { MobileSheet } from './MobileSheet'
import { Check, Copy } from 'lucide-react'

// Run info sheet: the run's markdown notes (nb.md) and config chips
// (nb.log_cfg), with a copyable-identifiers dialog behind the id chip.
export function MobileRunInfoSheet({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const run = useStore(s => s.runs.get(runId))
  const [idsOpen, setIdsOpen] = useState(false)

  if (!run) return null
  const name = runDisplayName(run.summary)
  const description = run.graph?.workflow_description
  const config = run.graph?.run_config ?? run.summary.run_config ?? null
  const configEntries = config ? Object.entries(config) : []

  return (
    <MobileSheet onClose={onClose} heightClass="h-[72vh]">
      <div className="flex shrink-0 items-baseline gap-2 px-4 pb-3">
        <span className="min-w-0 flex-1 truncate text-base font-semibold">{name}</span>
        <button
          onClick={() => setIdsOpen(true)}
          className="flex shrink-0 items-center gap-1.5 font-mono text-xs text-muted-foreground"
        >
          <Copy className="h-3 w-3" />
          {run.summary.id.slice(0, 8)}
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pb-10">
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Notes
          </div>
          {description ? (
            <div className="rounded-xl border border-border bg-card px-4 py-3.5">
              {/* Keep in lockstep with MobileGroupNotes' wrapper — the
                  two markdown surfaces must match. */}
              <div className="prose prose-sm max-w-none dark:prose-invert">
                <NeboMarkdown>{description}</NeboMarkdown>
              </div>
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">
              No notes — add them with <code className="rounded bg-muted px-1 py-0.5">nb.md(…)</code>
            </div>
          )}
        </div>
        <div>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
            Config
          </div>
          {configEntries.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              No config — log one with <code className="rounded bg-muted px-1 py-0.5">nb.log_cfg(…)</code>
            </div>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {configEntries.map(([k, v]) => (
                <span
                  key={k}
                  className="flex items-baseline gap-1.5 rounded-md bg-muted px-2 py-1 font-mono text-[11.5px]"
                >
                  <span className="text-muted-foreground">{k}</span>
                  <span className="text-foreground">{formatConfigValue(v)}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      {idsOpen && <RunIdsDialog runId={runId} onClose={() => setIdsOpen(false)} />}
    </MobileSheet>
  )
}

function RunIdsDialog({ runId, onClose }: { runId: string; onClose: () => void }) {
  const run = useStore(s => s.runs.get(runId))
  const runTree = useStore(s => s.runTree)
  const [copiedLabel, setCopiedLabel] = useState<string | null>(null)

  if (!run) return null
  const group = runTree.runs[runId]
  const rows: { label: string; value: string }[] = [
    { label: 'Run ID', value: run.summary.id },
    { label: 'Script path', value: run.summary.script_path },
    ...(group ? [{ label: 'Group', value: group }] : []),
    ...(run.summary.started_at ? [{ label: 'Started', value: run.summary.started_at }] : []),
  ]

  const copy = (label: string, value: string) => {
    navigator.clipboard?.writeText(value).then(() => {
      setCopiedLabel(label)
      setTimeout(() => setCopiedLabel(c => (c === label ? null : c)), 1500)
    })
  }

  return (
    <Modal open onClose={onClose} title="Run identifiers" widthClass="max-w-sm">
      <div className="flex flex-col gap-2.5 p-4">
        {rows.map(row => (
          <button
            key={row.label}
            onClick={() => copy(row.label, row.value)}
            className="flex items-center gap-2.5 rounded-[10px] border border-border bg-card px-3 py-2.5 text-left"
          >
            <div className="min-w-0 flex-1">
              <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                {row.label}
              </div>
              <div className="break-all font-mono text-xs leading-[1.4]">{row.value}</div>
            </div>
            {copiedLabel === row.label ? (
              <Check className="h-3.5 w-3.5 shrink-0 text-green-500" />
            ) : (
              <Copy className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            )}
          </button>
        ))}
      </div>
    </Modal>
  )
}

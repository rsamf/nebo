import { useEffect, useMemo, useState } from 'react'
import { useStore } from '@/store'
import { api } from '@/lib/api'
import type { RunSummary } from '@/lib/api'
import { childGroupsOf, membersOf } from '@/lib/runTree'
import { NeboMarkdown } from '@/components/shared/NeboMarkdown'
import { MobileGroupCard, MobileRunCard } from './MobileRunList'
import { Badge } from '@/components/ui/badge'
import { ArrowLeft, ChevronDown, ChevronRight, FileText } from 'lucide-react'

// Full-page render of one group: a Notes card (→ the notes screen),
// then subgroups, then member runs. Back walks up one level of the
// group hierarchy (root list at the top). Mobile counterpart of
// layout/GroupPage.
export function MobileGroupPage({ path }: { path: string }) {
  const runTree = useStore(s => s.runTree)
  const runs = useStore(s => s.runs)
  const selectGroup = useStore(s => s.selectGroup)
  const [notesOpen, setNotesOpen] = useState(false)

  // Leaving the page (navigating to a run or another group) resets this
  // naturally — the component unmounts.
  const docNames = runTree.groups[path]?.docs ?? []

  const byId = useMemo(
    () => new Map(Array.from(runs.values(), r => [r.summary.id, r.summary] as [string, RunSummary])),
    [runs],
  )
  const members = membersOf(runTree.runs, path, byId)
  const subgroups = childGroupsOf(runTree.groups, path)
  const exists = path in runTree.groups
  const leaf = path.split('/').pop() ?? path
  const parent = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : null

  if (notesOpen) {
    return <MobileGroupNotes path={path} docNames={docNames} onBack={() => setNotesOpen(false)} />
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2.5 border-b border-border px-4 pb-2.5 pt-3">
        <button
          onClick={() => selectGroup(parent)}
          aria-label={parent ? `Back to ${parent}` : 'Back to runs'}
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold">{leaf}</div>
          {/* Subtext is the parent path only — a root group would just
              repeat its own name here, so it shows nothing. */}
          {parent && (
            <div className="truncate font-mono text-[11px] text-muted-foreground">{parent}</div>
          )}
        </div>
        {docNames.length > 0 && (
          <button
            onClick={() => setNotesOpen(true)}
            aria-label={`Notes (${docNames.length})`}
            className="relative flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-muted-foreground"
          >
            <FileText className="h-[18px] w-[18px]" />
            <Badge className="absolute right-0 top-0 h-4 min-w-4 justify-center rounded-full px-1 py-0 text-[10px] leading-none tabular-nums">
              {docNames.length}
            </Badge>
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-10 pt-3.5">
        {!exists && (
          <div className="pb-3 text-sm text-muted-foreground">This group no longer exists.</div>
        )}

        {subgroups.map(g => (
          <MobileGroupCard key={g} path={g} />
        ))}
        {members.map(s => (
          <MobileRunCard key={s.id} run={s} />
        ))}

        {exists && subgroups.length === 0 && members.length === 0 && docNames.length === 0 && (
          <div className="px-1 py-2 text-xs text-muted-foreground">This group is empty.</div>
        )}
      </div>
    </div>
  )
}

// The group's markdown files as collapsible sections (README first, as
// served by the daemon). The first file starts expanded.
function MobileGroupNotes({
  path,
  docNames,
  onBack,
}: {
  path: string
  docNames: string[]
  onBack: () => void
}) {
  const [docs, setDocs] = useState<Record<string, string>>({})
  const [collapsed, setCollapsed] = useState<Set<string>>(
    () => new Set(docNames.slice(1)),
  )

  useEffect(() => {
    let cancelled = false
    Promise.all(
      docNames.map(name =>
        api
          .getGroupDoc(path, name)
          .then(content => [name, content] as const)
          .catch(() => [name, null] as const),
      ),
    ).then(pairs => {
      if (cancelled) return
      const out: Record<string, string> = {}
      for (const [name, content] of pairs) if (content != null) out[name] = content
      setDocs(out)
    })
    return () => {
      cancelled = true
    }
    // Re-fetch when the group or its doc set changes.
  }, [path, docNames.join('|')])

  const toggle = (name: string) =>
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2.5 border-b border-border px-4 pb-2.5 pt-3">
        <button
          onClick={onBack}
          aria-label="Back to group"
          className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-muted"
        >
          <ArrowLeft className="h-4 w-4" />
        </button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[15px] font-semibold">Notes</div>
          <div className="truncate font-mono text-[11px] text-muted-foreground">{path}</div>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-10 pt-3.5">
        {docNames.map(name => {
          const isCollapsed = collapsed.has(name)
          return (
            <div key={name} className="mb-2 overflow-hidden rounded-xl border border-border bg-card">
              <button
                onClick={() => toggle(name)}
                className="flex w-full items-center gap-2.5 px-4 py-3 text-left"
              >
                <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate font-mono text-[13px] font-medium">{name}</span>
                {isCollapsed
                  ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
              </button>
              {!isCollapsed && (
                <div className="border-t border-border px-4 py-3.5">
                  {docs[name] != null ? (
                    // Keep in lockstep with MobileRunInfoSheet's Notes
                    // wrapper — the two markdown surfaces must match.
                    <div className="prose prose-sm max-w-none dark:prose-invert">
                      <NeboMarkdown>{docs[name]}</NeboMarkdown>
                    </div>
                  ) : (
                    <div className="text-xs text-muted-foreground">Loading…</div>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

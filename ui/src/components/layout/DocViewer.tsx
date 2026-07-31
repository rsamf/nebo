import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { NeboMarkdown } from '@/components/shared/NeboMarkdown'
import { ScrollArea } from '@/components/ui/scroll-area'
import { FileText } from 'lucide-react'

/** Dedicated markdown viewer for one group doc, selected from the run tree.
 *  Rendered in place of the run detail view (desktop). */
export function DocViewer({ doc }: { doc: { group: string; name: string } }) {
  // Keyed remount resets the fetch state when the selected doc changes,
  // so the effect never has to setState synchronously.
  return <DocViewerInner key={`${doc.group}/${doc.name}`} doc={doc} />
}

function DocViewerInner({ doc }: { doc: { group: string; name: string } }) {
  const [content, setContent] = useState<string | null>(null)
  const [missing, setMissing] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.getGroupDoc(doc.group, doc.name)
      .then(text => {
        if (cancelled) return
        if (text == null) setMissing(true)
        else setContent(text)
      })
      .catch(() => { if (!cancelled) setMissing(true) })
    return () => { cancelled = true }
  }, [doc.group, doc.name])

  return (
    <ScrollArea className="h-full">
      {/* No back button: the run tree is always visible, so navigating
          away is a click on any other tree item. */}
      <div className="mx-auto max-w-3xl space-y-4 p-6">
        <div className="flex items-baseline gap-2">
          <FileText className="h-4 w-4 shrink-0 self-center text-muted-foreground" />
          <h1 className="min-w-0 truncate text-lg font-semibold">{doc.name}</h1>
          <span className="shrink-0 font-mono text-xs text-muted-foreground">{doc.group}</span>
        </div>
        {missing && (
          <div className="text-sm text-muted-foreground">This document no longer exists.</div>
        )}
        {content == null && !missing && (
          <div className="text-sm text-muted-foreground">Loading…</div>
        )}
        {content != null && (
          <div className="prose prose-sm max-w-none dark:prose-invert">
            <NeboMarkdown>{content}</NeboMarkdown>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

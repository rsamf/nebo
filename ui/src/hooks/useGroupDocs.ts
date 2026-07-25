import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

/** Fetch a group's markdown docs (name → content). Unfetchable docs are
 *  omitted. Shared by the desktop GroupPage and the mobile notes screen
 *  so error handling and refetch semantics can't drift. */
export function useGroupDocs(path: string, docNames: string[]): Record<string, string> {
  const [docs, setDocs] = useState<Record<string, string>>({})
  const docsKey = docNames.join('|')

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, docsKey])

  return docs
}

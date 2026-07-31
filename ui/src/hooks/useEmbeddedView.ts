import { useEffect, useState } from 'react'
import { parseRef } from '@/lib/refs'

/**
 * Embedded ("iframe-friendly") view kinds. The kind is inferred from
 * which URL params are present — there is no `view=` discriminator:
 *
 *   ?ref=nebo://run/<id>[/<loggable>[/<name>]]  → canonical-reference form
 *                              (bare run ref → 'run'; loggable ref → 'node')
 *   ?run=X                   → 'run'    (full DAG + timeline)
 *   ?run=X&dag               → 'dag'    (graph only)
 *   ?run=X&flat              → 'flat'   (loggable cards; same as desktop flat view)
 *   ?run=X&node=Y            → 'node'   (single node detail)
 *   ?run=X&text              → 'text'   (text panel; &node=Y filters)
 *   ?run=X&text=NAME         → 'text'   (single named text stream)
 *   ?run=X&metrics           → 'metrics' (metrics gallery; &node=Y filters)
 *   ?run=X&metric=NAME       → 'metric'  (single metric; &node=Y filters)
 *   ?run=X&images            → 'images'
 *   ?run=X&image=NAME        → 'image'
 *   ?run=X&audios            → 'audios'
 *   ?run=X&audio=NAME        → 'audio'
 */
export type EmbeddedKind =
  | 'run'
  | 'dag'
  | 'flat'
  | 'node'
  | 'text'
  | 'metrics'
  | 'metric'
  | 'images'
  | 'image'
  | 'audios'
  | 'audio'

export interface EmbeddedView {
  kind: EmbeddedKind
  runId: string
  // Optional: a node identifier — accepts either the canonical loggable_id
  // (e.g. "module.Class.method") or a bare function name. The view resolves
  // to the matching loggable at render time.
  nodeRef: string | null
  // For single-item kinds (metric / image / audio): the item name.
  name: string | null
}

function parse(): EmbeddedView | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)

  // Canonical-reference form: `?ref=nebo://run/<id>[/<loggable>[/<name>]]`.
  // Maps onto the param kinds: a bare run ref → the run dashboard, a
  // loggable (with or without a stream name) → the node detail card.
  // Group refs have no embed rendering.
  const refRaw = params.get('ref')
  if (refRaw) {
    const ref = parseRef(refRaw)
    if (ref?.kind === 'run') {
      if (ref.loggableId) return { kind: 'node', runId: ref.runId, nodeRef: ref.loggableId, name: ref.name }
      return { kind: 'run', runId: ref.runId, nodeRef: null, name: null }
    }
    return null
  }

  const runId = params.get('run')
  if (!runId) return null

  const nodeRef = params.get('node')

  // Singular-name kinds: `?metric=loss` etc. A non-empty value selects one item.
  const metric = params.get('metric')
  if (metric) return { kind: 'metric', runId, nodeRef, name: metric }
  const image = params.get('image')
  if (image) return { kind: 'image', runId, nodeRef, name: image }
  const audio = params.get('audio')
  if (audio) return { kind: 'audio', runId, nodeRef, name: audio }

  // Text: `?text` (bare) shows the panel, `?text=NAME` one named stream.
  if (params.has('text')) {
    return { kind: 'text', runId, nodeRef, name: params.get('text') || null }
  }

  // Plural-flag kinds: presence of the key (any value, including empty)
  // activates the gallery / panel. Order matters only for stable kind
  // selection when callers accidentally combine flags.
  if (params.has('metrics')) return { kind: 'metrics', runId, nodeRef, name: null }
  if (params.has('images')) return { kind: 'images', runId, nodeRef, name: null }
  if (params.has('audios')) return { kind: 'audios', runId, nodeRef, name: null }
  if (params.has('dag')) return { kind: 'dag', runId, nodeRef, name: null }
  if (params.has('flat')) return { kind: 'flat', runId, nodeRef, name: null }

  // Bare `?run=X&node=Y` → single-node detail (no slice flag).
  if (nodeRef) return { kind: 'node', runId, nodeRef, name: null }

  // Bare `?run=X` → full run dashboard.
  return { kind: 'run', runId, nodeRef: null, name: null }
}

/**
 * Read the URL query params that activate an embedded ("iframe-friendly")
 * view. When this returns a non-null value, the App should render only the
 * requested slice of a run instead of the full sidebar/detail layout.
 *
 * Listens to popstate so navigation within the SPA is reflected.
 */
export function useEmbeddedView(): EmbeddedView | null {
  const [view, setView] = useState<EmbeddedView | null>(parse)
  useEffect(() => {
    const handler = () => setView(parse())
    window.addEventListener('popstate', handler)
    return () => window.removeEventListener('popstate', handler)
  }, [])
  return view
}

/**
 * Resolve a `node` URL param against the live graph. Tries exact loggable_id
 * match first, then falls back to func_name. Returns null if no match.
 */
export function resolveNodeRef(
  nodeRef: string | null,
  nodes: Record<string, { func_name: string }> | undefined,
): string | null {
  if (!nodeRef || !nodes) return null
  if (nodes[nodeRef]) return nodeRef
  for (const [id, n] of Object.entries(nodes)) {
    if (n.func_name === nodeRef) return id
  }
  return null
}

interface EmbeddedUrlSpec {
  runId: string
  // Optional single-item targets; only one of `metric`/`image`/`audio`
  // should be set at a time (the parser above picks the first present).
  node?: string
  metric?: string
  image?: string
  audio?: string
  // Text: `true` → the whole text panel; a string → one named stream.
  text?: string | boolean
  // Panel-style slice (no single item).
  dag?: boolean
  flat?: boolean
}

/**
 * Build an iframe-friendly URL using the same query-param schema that
 * `parse()` above consumes. Producer side of the same contract — keeps
 * the URL shape in one place.
 */
export function buildEmbeddedUrl(spec: EmbeddedUrlSpec): string {
  const params = new URLSearchParams()
  params.set('run', spec.runId)
  if (spec.metric) params.set('metric', spec.metric)
  if (spec.image) params.set('image', spec.image)
  if (spec.audio) params.set('audio', spec.audio)
  if (typeof spec.text === 'string') params.set('text', spec.text)
  else if (spec.text) params.set('text', '')
  if (spec.dag) params.set('dag', '')
  if (spec.flat) params.set('flat', '')
  if (spec.node) params.set('node', spec.node)
  return `${window.location.origin}/?${params.toString()}`
}

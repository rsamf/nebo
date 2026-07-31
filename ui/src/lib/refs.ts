// `nebo://` resource references — the canonical ID for every resource.
// TypeScript twin of `nebo/core/refs.py`; keep the two in lockstep.
//
//   nebo://run/<run_id>                          a run
//   nebo://run/<run_id>/<loggable_id>            a loggable within it
//   nebo://run/<run_id>/<loggable_id>/<name...>  a named stream
//   <any run form>@<step>                        a datapoint at a step
//   nebo://group/<path>                          a group
//
// The loggable segment is always exactly one path segment (`__global__`,
// `__agent__`, or a Python qualname — dots, never slashes); everything
// after it is the stream name, which may contain '/'. `?step=<n>` is a
// legacy alias for `@<step>`.

export const NEBO_SCHEME = 'nebo://'

export interface RunRef {
  kind: 'run'
  runId: string
  loggableId: string | null
  name: string | null
  step: number | null
}

export interface GroupRef {
  kind: 'group'
  path: string
}

export type NeboRef = RunRef | GroupRef

export function parseRef(href: string): NeboRef | null {
  if (!href.startsWith(NEBO_SCHEME)) return null
  const rest = href.slice(NEBO_SCHEME.length)
  const qIdx = rest.indexOf('?')
  let pathPart = qIdx === -1 ? rest : rest.slice(0, qIdx)
  const query = qIdx === -1 ? '' : rest.slice(qIdx + 1)

  let step: number | null = null
  if (query) {
    const raw = new URLSearchParams(query).get('step')
    if (raw !== null && raw !== '' && Number.isInteger(Number(raw))) {
      step = Number(raw)
    }
  }

  if (pathPart.startsWith('group/')) {
    const path = pathPart.slice('group/'.length).replace(/^\/+|\/+$/g, '')
    return path ? { kind: 'group', path } : null
  }

  if (!pathPart.startsWith('run/')) return null
  pathPart = pathPart.slice('run/'.length).replace(/^\/+|\/+$/g, '')
  if (!pathPart) return null

  // `@<step>` binds to the end of the whole path; only a numeric tail
  // counts (an `@` may legally appear inside a stream name).
  const at = pathPart.lastIndexOf('@')
  if (at !== -1) {
    const tail = pathPart.slice(at + 1)
    if (/^-?\d+$/.test(tail)) {
      step = Number(tail)
      pathPart = pathPart.slice(0, at)
    }
  }

  const segments = pathPart.split('/').filter(Boolean)
  if (segments.length === 0) return null
  return {
    kind: 'run',
    runId: segments[0],
    loggableId: segments.length > 1 ? segments[1] : null,
    name: segments.length > 2 ? segments.slice(2).join('/') : null,
    step,
  }
}

export function formatRef(ref: NeboRef): string {
  if (ref.kind === 'group') return `${NEBO_SCHEME}group/${ref.path.replace(/^\/+|\/+$/g, '')}`
  const parts = [ref.runId]
  if (ref.loggableId) {
    parts.push(ref.loggableId)
    if (ref.name) parts.push(ref.name)
  }
  let out = `${NEBO_SCHEME}run/${parts.join('/')}`
  if (ref.step != null) out += `@${ref.step}`
  return out
}

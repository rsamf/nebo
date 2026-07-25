import { authHeaders, getAuthToken, setUnauthorized } from './auth'

const BASE = ''

function noteAuthStatus(status: number): void {
  // 401 means the daemon enforces auth and our token (if any) didn't
  // match. Surface it so the UI can swap the "Reconnecting" banner for
  // a token prompt. A successful response clears the flag — covers the
  // case where the user just submitted a fresh token.
  if (status === 401) {
    setUnauthorized(true)
  } else if (status >= 200 && status < 300) {
    setUnauthorized(false)
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { ...authHeaders() },
  })
  noteAuthStatus(res.status)
  if (!res.ok) throw new Error(`GET ${path}: ${res.status}`)
  return res.json()
}

async function getText(path: string): Promise<string | null> {
  const res = await fetch(`${BASE}${path}`, { headers: { ...authHeaders() } })
  noteAuthStatus(res.status)
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`GET ${path}: ${res.status}`)
  return res.text()
}

/** Encode a group path for a URL, keeping `/` separators intact. */
export function encodeGroupPath(path: string): string {
  return path.split('/').map(encodeURIComponent).join('/')
}

export interface RunSummary {
  id: string
  script_path: string
  args: string[]
  started_at: string | null
  // Epoch seconds of the most recent observed event. There is no ended_at:
  // a run is never known to be "done", so recency is the only liveness fact.
  last_event_at: number | null
  node_count: number
  edge_count: number
  log_count: number
  run_name: string | null
  run_config?: Record<string, unknown>
  metric_series_count?: number
  latest_step?: number | null
}

/** Seconds of quiet after which a run stops reading as "live". */
export const LIVE_RECENCY_S = 60

/** A run reads as live when it emitted an event within the last minute.
 *  Presentational only — there is no completed/crashed state to key off. */
export function isRunLive(
  summary: { last_event_at?: number | null } | null | undefined,
): boolean {
  const t = summary?.last_event_at
  return typeof t === 'number' && Date.now() / 1000 - t < LIVE_RECENCY_S
}

// ── Run tree (groups) ────────────────────────────────────────────────

export interface TreeData {
  // group path -> { docs: [filename, ...] } (README.md first)
  groups: Record<string, { docs: string[] }>
  // run_id -> group path; runs absent from this map are at the root.
  runs: Record<string, string>
}

export const EMPTY_TREE: TreeData = { groups: {}, runs: {} }

/** A parsed `nebo://` deep link (used in group docs). */
export type NeboLink =
  | { kind: 'run'; runId: string; step: number | null }
  | { kind: 'group'; path: string }

/** Parse a `nebo://run/<id>[?step=<n>]` or `nebo://group/<path>` href, or
 *  null if it isn't a recognized nebo link. */
export function parseNeboLink(href: string): NeboLink | null {
  if (!href.startsWith('nebo://')) return null
  const [pathPart, query] = href.slice('nebo://'.length).split('?')
  if (pathPart.startsWith('run/')) {
    const runId = pathPart.slice('run/'.length)
    if (!runId) return null
    let step: number | null = null
    if (query) {
      const raw = new URLSearchParams(query).get('step')
      if (raw !== null && raw !== '' && Number.isFinite(Number(raw))) {
        step = Number(raw)
      }
    }
    return { kind: 'run', runId, step }
  }
  if (pathPart.startsWith('group/')) {
    const path = pathPart.slice('group/'.length).replace(/\/+$/, '')
    return path ? { kind: 'group', path } : null
  }
  return null
}

export interface GraphData {
  nodes: Record<string, {
    name: string
    func_name: string
    docstring: string | null
    exec_count: number
    is_source: boolean
    params: Record<string, unknown>
    progress: { current: number; total: number; name?: string } | null
    group: string | null
    ui_hints: Record<string, unknown> | null
  }>
  edges: { source: string; target: string }[]
  workflow_description: string | null
  ui_config?: UiConfig | null
  run_config?: Record<string, unknown> | null
}

export interface UiConfig {
  layout?: 'horizontal' | 'vertical'
  view?: 'dag' | 'flat'
  collapsed?: boolean
  minimap?: boolean
  theme?: 'dark' | 'light'
  tracker?: 'time' | 'step'
}

export interface LogEntry {
  timestamp: number
  node: string | null
  name: string
  message: string
  level: string
  step: number | null
}

export interface BitmaskEntry {
  width: number
  height: number
  data: string  // base64 PNG, grayscale; nonzero pixels are mask-on
}

export type MetricType = 'line' | 'bar' | 'scatter' | 'pie' | 'histogram'

export interface MetricEntry {
  step: number | null
  value: unknown
  tags: string[]
  timestamp: number
  // Set on log_scatter / log_histogram emissions when the user passed
  // colors=True. The chart components read this to decide whether to
  // distinguish labels by palette color (true) or by shape only (false).
  colors?: boolean
}

export interface LoggableMetricSeries {
  type: MetricType
  entries: MetricEntry[]
}

export interface LabelGroup<T> {
  data: T
  color: string
}

export interface PolygonsLabelGroup extends LabelGroup<number[][][]> {
  // True (default): fill the interior of each polygon with `color` at
  // the rendered opacity. False: stroke the outline only.
  fill?: boolean
}

export interface LabelsPayload {
  points?: LabelGroup<number[][]>[]       // each entry: list of [x, y]
  boxes?: LabelGroup<number[][]>[]        // each entry: list of [x1, y1, x2, y2]
  circles?: LabelGroup<number[][]>[]      // each entry: list of [x, y, r]
  polygons?: PolygonsLabelGroup[]         // each entry: list of polygons
  bitmasks?: LabelGroup<BitmaskEntry[]>[]
}

// A fired alert on a run: code-fired (`nb.alert`, triggered_by "code") or
// rule-fired (CLI/MCP condition rules, triggered_by "cli").
export interface AlertEntry {
  title: string
  text: string
  level: number          // 10 debug / 20 info / 30 warn / 40 error
  level_name: string
  triggered_by: string
  loggable_id: string | null
  timestamp: number
  condition?: string     // rule-fired only: display string of the condition
  step?: number | null
  value?: unknown
}

export interface NodeDetail {
  name: string
  func_name: string
  docstring: string | null
  exec_count: number
  is_source: boolean
  params: Record<string, unknown>
  recent_logs: unknown[]
  metrics: Record<string, LoggableMetricSeries>
  progress: { current: number; total: number; name?: string } | null
}

// Loggable = node-or-non-node addressable thing that can receive logs, metrics,
// images, audio, etc. `graph.nodes` only contains node-kind loggables; the
// per-run store slices (loggableMetrics, etc.) are keyed by loggableId and may
// contain any kind. Non-node kinds: `global` (user logs outside any node),
// `agent` (entries authored over MCP by an external agent).
export type LoggableState = NodeDetail & {
  kind: 'node' | 'global' | 'agent'
  loggable_id: string
}

export interface LoggableRegistration {
  loggable_id: string
  kind: 'node' | 'global' | 'agent'
  func_name?: string
  docstring?: string | null
  group?: string | null
  ui_hints?: Record<string, unknown> | null
}

export const api = {
  health: () => get<{ status: string; active_run: string | null; total_runs: number }>('/health'),

  listRuns: () => get<{ runs: RunSummary[]; active_run: string | null }>('/runs'),
  getRun: (id: string) => get<RunSummary>(`/runs/${id}`),
  getRunGraph: (id: string) => get<GraphData>(`/runs/${id}/graph`),
  getRunLogs: (id: string, opts?: { loggable_id?: string; limit?: number }) => {
    const params = new URLSearchParams()
    if (opts?.loggable_id) params.set('loggable_id', opts.loggable_id)
    if (opts?.limit) params.set('limit', String(opts.limit))
    const qs = params.toString()
    return get<{ logs: LogEntry[] }>(`/runs/${id}/logs${qs ? `?${qs}` : ''}`)
  },
  getRunMetrics: (id: string) => get<{ metrics: Record<string, Record<string, LoggableMetricSeries>> }>(`/runs/${id}/metrics`),
  getRunImages: (id: string) => get<{ images: Record<string, Array<{ node: string; media_id: string; name: string; step: number | null; timestamp: number; labels?: LabelsPayload | null }>> }>(`/runs/${id}/images`),
  getRunAudio: (id: string) => get<{ audio: Record<string, Array<{ node: string; media_id: string; name: string; sr: number; step: number | null; timestamp: number }>> }>(`/runs/${id}/audio`),
  getRunAlerts: (id: string) => get<{ alerts: AlertEntry[] }>(`/runs/${id}/alerts`),
  // Media is served as raw immutable bytes (ETag = content-addressed
  // media_id), so <img>/<audio> reference the URL directly and the browser
  // cache does the rest. Tokens ride as a query param — media elements
  // can't send custom headers.
  mediaUrl: (runId: string, mediaId: string) => {
    const token = getAuthToken()
    const qs = token ? `?token=${encodeURIComponent(token)}` : ''
    return `${BASE}/runs/${runId}/media/${mediaId}${qs}`
  },
  getTree: () => get<TreeData>('/tree'),
  getGroupDoc: (path: string, name: string) =>
    getText(`/groups/${encodeGroupPath(path)}/docs/${encodeURIComponent(name)}`),
}

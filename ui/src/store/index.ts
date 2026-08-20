import { create } from 'zustand'
import type { RunSummary, GraphData, TextEntry, LabelsPayload, MetricType, MetricEntry, LoggableMetricSeries, TreeData, AlertEntry, ActionFrame, BodyModelManifest } from '@/lib/api'
import { EMPTY_TREE, parseNeboLink } from '@/lib/api'
import type { WsEvent } from '@/lib/ws'
import { assignColor } from '@/lib/colors'

export interface NodeState {
  name: string
  funcName: string
  docstring: string | null
  params: Record<string, unknown>
  executionCount: number
  isSource: boolean
  progress: { current: number; total: number; name?: string } | null
  inDag: boolean
}

export interface LoggableState extends NodeState {
  kind: 'node' | 'global' | 'agent'
  loggableId: string
}

export interface Settings {
  theme: 'dark' | 'light'
  showMinimap: boolean
  showControls: boolean
  hideTabsOnDrag: boolean
  // EMA factor in [0, 1]. 0 = no smoothing, → 1 = heavy smoothing.
  // Applied per-dataset on every line chart at render time; raw values
  // in the store are unchanged.
  lineSmoothing: number
  histogramSmoothing: number
  // Number of bins shared across labels in every histogram chart.
  histogramBinCount: number
  // Active-point opacity for scatter charts (0–1). Applies in both
  // single-run and comparison views. Dimmed (filtered-out) points keep
  // their own ~25% alpha treatment.
  scatterPointOpacity: number
  // Scatter point size scale (0–1). 1 keeps the original radii (~7px
  // active, ~4px default, ~3px dimmed); 0.5 halves them.
  scatterPointSize: number
  // --- Action scenes (3D) ---
  // Opacity of a body model's visual geometry (0–1).
  bodyOpacity: number
  // Collision geometry is authored for the physics engine, not the eye,
  // so it is off by default and drawn translucent when shown.
  showCollision: boolean
  collisionOpacity: number
  // Tint each instance in a scene with its own color so a reference pose
  // reads apart from a policy-actuated one. Single-instance scenes keep
  // the model's own materials (see ActionCard).
  tintInstances: boolean
}

const SETTINGS_KEY = 'gb_settings'

export const DEFAULT_HISTOGRAM_BIN_COUNT = 30

const DEFAULT_SETTINGS: Settings = {
  theme: 'dark',
  showMinimap: false,
  showControls: true,
  hideTabsOnDrag: false,
  lineSmoothing: 0,
  histogramSmoothing: 0,
  histogramBinCount: DEFAULT_HISTOGRAM_BIN_COUNT,
  scatterPointOpacity: 0.8,
  scatterPointSize: 0.5,
  bodyOpacity: 1,
  showCollision: false,
  collisionOpacity: 0.35,
  tintInstances: true,
}

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      return { ...DEFAULT_SETTINGS, ...parsed }
    }
  } catch { /* ignore */ }
  return { ...DEFAULT_SETTINGS }
}

function saveSettings(settings: Settings) {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
  } catch { /* ignore */ }
}

const initialSettings = loadSettings()

// Apply persisted theme on load
document.documentElement.classList.toggle('dark', initialSettings.theme === 'dark')

export type NodeTab = 'text' | 'metrics' | 'images' | 'audio' | 'actions'

export interface ImageEntry {
  node: string
  mediaId: string
  name: string
  step: number | null
  timestamp: number
  labels?: LabelsPayload | null
}

export interface LabelKeySetting {
  visible: boolean
  opacity: number  // 0-100
}

const LABEL_SETTINGS_KEY = 'nebo_label_settings'

function loadLabelSettings(): Record<string, LabelKeySetting> {
  try {
    const raw = localStorage.getItem(LABEL_SETTINGS_KEY)
    if (raw) return JSON.parse(raw)
  } catch { /* ignore */ }
  return {}
}

function saveLabelSettings(s: Record<string, LabelKeySetting>) {
  try {
    localStorage.setItem(LABEL_SETTINGS_KEY, JSON.stringify(s))
  } catch { /* ignore */ }
}

export interface AudioEntry {
  node: string
  mediaId: string
  name: string
  sr: number
  step: number | null
  timestamp: number
}

export type TimelineMode = 'time' | 'step'

export interface TimelineState {
  mode: TimelineMode
  step: number | null
  time: number | null          // single playhead timestamp
  selectedStream: string | null
  // Playback drives the shared step forward so a 3D scene, its metrics
  // and its text all advance together. Lives on the tracker because the
  // playhead does; see usePlayback.
  playing: boolean
  fps: number
}

export interface ComparisonGroup {
  id: string
  title: string
  runIds: string[]
  createdAt: Date
}

export interface RunState {
  summary: RunSummary
  graph: GraphData | null
  texts: TextEntry[]
  loggableMetrics: Record<string, Record<string, LoggableMetricSeries>>
  loggableImages: Record<string, ImageEntry[]>
  loggableAudio: Record<string, AudioEntry[]>
  // Action modality: scene frames per loggable, plus the run-level body
  // models they reference. Models are run-scoped, not per-loggable — one
  // node can publish a model that another node's scene renders.
  loggableActions: Record<string, ActionFrame[]>
  bodyModels: Record<string, BodyModelManifest>
  // Fired alerts. Hydrated lazily (the mobile alerts sheet fetches on
  // open via setRunAlerts) and appended live from WS `alert` events.
  alerts: AlertEntry[]
  loaded: boolean
  globalLoggable?: { loggableId: string; kind: 'global' }
  agentLoggable?: { loggableId: string; kind: 'agent' }
}

interface NeboStore {
  // Connection
  connected: boolean
  reconnecting: boolean
  setConnectionStatus: (connected: boolean, reconnecting: boolean) => void

  // Runs
  runs: Map<string, RunState>
  selectedRunId: string | null
  activeRunId: string | null
  // True once the initial "most recent run" auto-select has happened.
  // Later setRuns calls (the 5 s poll) must not re-select after the
  // user deliberately navigated back to the run list (mobile) or
  // deselected (comparison removal).
  autoSelected: boolean

  // Runs whose REST hydration is in flight — the UI shows a "loading
  // history…" hint so a partially-rendered run reads as loading, not done.
  hydratingRuns: Set<string>
  setRunHydrating: (runId: string, hydrating: boolean) => void

  runColors: Map<string, string>
  nextColorIndex: number
  setRunColor: (runId: string, color: string) => void
  getOrAssignRunColor: (runId: string) => string

  // Comparison
  selectedForCompare: Set<string>
  comparisonGroups: Map<string, ComparisonGroup>
  toggleSelectForCompare: (runId: string) => void
  createComparisonGroup: (runIds: string[]) => string
  removeComparisonGroup: (groupId: string) => void

  // Run tree (groups) — a read-only view over the daemon's group hierarchy.
  // Distinct from GraphData.nodes[].group (intra-run node clustering).
  runTree: TreeData
  setRunTree: (tree: TreeData) => void
  // Mobile-only: a group page is shown when selectedGroup is set; it and
  // selectedRunId are mutually exclusive (selecting one clears the other).
  // Desktop has no group page — groups only expand/collapse in the tree.
  selectedGroup: string | null
  selectGroup: (path: string | null) => void
  // Desktop: a group doc selected from the run tree, rendered by the
  // dedicated markdown viewer in place of the run detail view.
  selectedDoc: { group: string; name: string } | null
  selectDoc: (doc: { group: string; name: string } | null) => void
  expandedGroups: Set<string>
  toggleGroupExpanded: (path: string) => void
  // Handle a nebo:// deep link clicked in a group doc.
  navigateNebo: (href: string) => void
  // Ephemeral notice (e.g. a nebo:// target that no longer exists).
  notice: string | null
  setNotice: (msg: string | null) => void
  // A nebo:// deep link's target within a run, pending resolution by the
  // flat view: it switches to the owning tab, scrolls the card into view,
  // and highlights it. `name` is the stream/metric/media name when the
  // link addressed one; null means "this loggable's first card".
  pendingNavTarget: { loggableId: string; name: string | null } | null
  setPendingNavTarget: (target: { loggableId: string; name: string | null } | null) => void

  // Node interaction (graph view)
  layoutTrigger: number
  dagDirection: 'TB' | 'LR'
  toggleDagDirection: () => void

  // Tracks which runs have already had their server-sent ui_config
  // applied to the UI defaults, so user overrides aren't clobbered
  // on graph refetch.
  appliedUiConfigRuns: Set<string>

  // Node positions & sizes (per run, session-only)
  nodePositions: Map<string, Map<string, { x: number; y: number }>>
  nodeSizes: Map<string, Map<string, { width: number; height: number }>>
  resizingNodeId: string | null
  toggleNodeResize: (nodeId: string) => void
  updateNodeSize: (runId: string, nodeId: string, size: { width: number; height: number }) => void

  // User overrides for node collapsed state, per (run, node). Absence
  // means "fall back to the ui_hints.collapsed value the SDK sent for
  // that node" — so `@nb.fn(ui={"collapsed": True})` seeds the initial
  // state but a manual toggle takes precedence.
  collapsedNodes: Map<string, Map<string, boolean>>
  toggleNodeCollapsed: (runId: string, nodeId: string) => void

  // User-selected tab per (run, loggable). Absence falls back to
  // ui_hints.default_tab (text default) the way LoggableTabContainer
  // resolves it. Lifted into the store so the export feature can
  // honor the user's currently-clicked tab in the diagram render.
  selectedTabs: Map<string, Map<string, NodeTab>>
  setSelectedTab: (runId: string, loggableId: string, tab: NodeTab) => void

  // Soft cap applied while an export is rendering its offscreen tree —
  // tab panels (text / images / audio / metrics) read this and slice
  // their item lists down to N. `null` disables the cap. The export
  // orchestrator sets this before mounting and resets it in finally,
  // so the live UI is only affected during the brief export window.
  exportEntryLimit: number | null
  setExportEntryLimit: (limit: number | null) => void

  // View mode: 'flat' (default, all loggable cards) or 'graph' (DAG)
  viewMode: 'graph' | 'flat'
  setViewMode: (mode: 'graph' | 'flat') => void

  // Right panel (Settings only)
  rightPanelOpen: boolean
  toggleRightPanel: () => void

  // Timeline
  timeline: TimelineState
  setTimelineMode: (mode: TimelineMode) => void
  setTimelineStep: (step: number | null) => void
  // Select a step as the active playhead: flips mode to 'step' and sets
  // the step in one update. The shared "click/scrub a chart point"
  // action used by LineMetric, ScatterMetric and the mobile chart gate.
  selectTimelineStep: (step: number) => void
  setTimelineTime: (time: number | null) => void
  setSelectedStream: (path: string | null) => void
  // Starting playback flips to step mode — the same flip clicking a chart
  // datapoint performs, since a playhead advancing through time and one
  // advancing through steps are different domains.
  setPlaying: (playing: boolean) => void
  setFps: (fps: number) => void

  // Settings
  settings: Settings

  // Label-key visibility + opacity, keyed by `${loggableName}|${imageName}|${key}`.
  labelKeySettings: Record<string, LabelKeySetting>
  registerLabelKey: (loggable: string, image: string, key: string) => void
  setLabelKeyVisible: (loggable: string, image: string, key: string, visible: boolean) => void
  setLabelKeyOpacity: (loggable: string, image: string, key: string, opacity: number) => void

  // Actions
  setRuns: (summaries: RunSummary[], activeRunId: string | null) => void
  updateRunSummary: (summary: RunSummary) => void
  setRunGraph: (runId: string, graph: GraphData) => void
  setRunTexts: (runId: string, texts: TextEntry[]) => void
  appendRunText: (runId: string, text: TextEntry) => void
  setRunMetrics: (runId: string, metrics: Record<string, Record<string, LoggableMetricSeries>>) => void
  setRunImages: (runId: string, images: Record<string, ImageEntry[]>) => void
  setRunAudio: (runId: string, audio: Record<string, AudioEntry[]>) => void
  setRunActions: (runId: string, actions: Record<string, ActionFrame[]>, bodyModels: Record<string, BodyModelManifest>, replace?: boolean) => void
  setRunAlerts: (runId: string, alerts: AlertEntry[]) => void
  appendMetric: (runId: string, loggableId: string, name: string, entry: MetricEntry, type: MetricType) => void
  updateNodeProgress: (runId: string, nodeId: string, progress: { current: number; total: number; name?: string } | null) => void
  incrementNodeExecCount: (runId: string, loggableId: string) => void
  addEdge: (runId: string, source: string, target: string) => void
  setWorkflowDescription: (runId: string, description: string) => void

  selectRun: (runId: string | null) => void
  requestLayout: () => void

  updateNodePosition: (runId: string, nodeId: string, pos: { x: number; y: number }) => void
  resetLayout: (runId: string) => void

  updateSetting: <K extends keyof Settings>(key: K, value: Settings[K]) => void

  processWsEvents: (runId: string, events: WsEvent[]) => void
}

export const useStore = create<NeboStore>((set, get) => ({
  connected: false,
  reconnecting: false,
  setConnectionStatus: (connected, reconnecting) => set({ connected, reconnecting }),

  runs: new Map(),
  selectedRunId: null,
  activeRunId: null,
  autoSelected: false,

  hydratingRuns: new Set<string>(),
  setRunHydrating: (runId, hydrating) => set(state => {
    if (state.hydratingRuns.has(runId) === hydrating) return {}
    const next = new Set(state.hydratingRuns)
    if (hydrating) next.add(runId)
    else next.delete(runId)
    return { hydratingRuns: next }
  }),

  runColors: new Map(),
  nextColorIndex: 0,
  setRunColor: (runId, color) => set(state => {
    const next = new Map(state.runColors)
    next.set(runId, color)
    return { runColors: next }
  }),
  getOrAssignRunColor: (runId) => {
    const state = get()
    const existing = state.runColors.get(runId)
    if (existing) return existing
    const color = assignColor(state.nextColorIndex)
    const next = new Map(state.runColors)
    next.set(runId, color)
    set({ runColors: next, nextColorIndex: state.nextColorIndex + 1 })
    return color
  },

  selectedForCompare: new Set<string>(),
  comparisonGroups: new Map(),
  toggleSelectForCompare: (runId) => set(state => {
    const next = new Set(state.selectedForCompare)
    if (next.has(runId)) next.delete(runId)
    else next.add(runId)
    return { selectedForCompare: next }
  }),
  createComparisonGroup: (runIds) => {
    const id = `cmp:${Date.now()}`
    const state = get()
    const names = runIds.map(rid => {
      const run = state.runs.get(rid)
      return run?.summary.run_name || (run?.summary.script_path.split('/').pop() ?? rid)
    })
    const title = names.length <= 2
      ? `Compare: ${names.join(' vs ')}`
      : `Compare: ${names[0]} vs ${names[1]} +${names.length - 2}`
    const group: ComparisonGroup = { id, title, runIds, createdAt: new Date() }
    const next = new Map(state.comparisonGroups)
    next.set(id, group)
    set({ comparisonGroups: next, selectedForCompare: new Set() })
    return id
  },
  removeComparisonGroup: (groupId) => set(state => {
    const next = new Map(state.comparisonGroups)
    next.delete(groupId)
    const updates: Partial<NeboStore> = { comparisonGroups: next }
    if (state.selectedRunId === groupId) updates.selectedRunId = null
    return updates
  }),

  layoutTrigger: 0,
  // Desktop defaults to a horizontal DAG; mobile's canvas hardcodes
  // top-to-bottom independently (MobileDagCanvas).
  dagDirection: 'LR',
  toggleDagDirection: () => set(state => ({
    dagDirection: state.dagDirection === 'TB' ? 'LR' : 'TB',
    layoutTrigger: state.layoutTrigger + 1,
  })),

  appliedUiConfigRuns: new Set<string>(),

  nodePositions: new Map(),
  nodeSizes: new Map(),
  resizingNodeId: null,
  toggleNodeResize: (nodeId) => set(state => ({
    resizingNodeId: state.resizingNodeId === nodeId ? null : nodeId,
  })),
  updateNodeSize: (runId, nodeId, size) => set(state => {
    const outer = new Map(state.nodeSizes)
    const inner = new Map(outer.get(runId) ?? [])
    inner.set(nodeId, size)
    outer.set(runId, inner)
    return { nodeSizes: outer }
  }),

  selectedTabs: new Map(),
  setSelectedTab: (runId, loggableId, tab) => set(state => {
    const outer = new Map(state.selectedTabs)
    const inner = new Map(outer.get(runId) ?? [])
    inner.set(loggableId, tab)
    outer.set(runId, inner)
    return { selectedTabs: outer }
  }),

  exportEntryLimit: null,
  setExportEntryLimit: (limit) => set({ exportEntryLimit: limit }),

  collapsedNodes: new Map(),
  toggleNodeCollapsed: (runId, nodeId) => set(state => {
    const outer = new Map(state.collapsedNodes)
    const inner = new Map(outer.get(runId) ?? [])
    const current = inner.get(nodeId)
    let next: boolean
    if (current === undefined) {
      // No prior override — pull the hint default off the registered
      // node and flip it, so the first toggle always changes the
      // visible state regardless of what the SDK seeded.
      const hint = state.runs.get(runId)?.graph?.nodes[nodeId]?.ui_hints
      const hintCollapsed = !!(hint && (hint as { collapsed?: unknown }).collapsed === true)
      next = !hintCollapsed
    } else {
      next = !current
    }
    inner.set(nodeId, next)
    outer.set(runId, inner)
    // Node height is changing — re-flow the DAG.
    return { collapsedNodes: outer, layoutTrigger: state.layoutTrigger + 1 }
  }),

  // Default to the Flat view on every screen size; the DAG view is
  // opt-in via the view switcher or nb.ui(view="dag").
  viewMode: 'flat' as 'graph' | 'flat',
  setViewMode: (mode) => set({ viewMode: mode }),

  // Open by default: the right panel is the home of a run's markdown,
  // config, and settings on desktop.
  rightPanelOpen: true,
  toggleRightPanel: () => set(state => ({ rightPanelOpen: !state.rightPanelOpen })),

  timeline: {
    mode: 'time', step: null, time: null, selectedStream: null,
    playing: false, fps: 30,
  },
  setTimelineMode: (mode) => set(state => ({ timeline: { ...state.timeline, mode } })),
  setTimelineStep: (step) => set(state => ({ timeline: { ...state.timeline, step } })),
  selectTimelineStep: (step) => set(state => ({ timeline: { ...state.timeline, mode: 'step', step } })),
  setTimelineTime: (time) => set(state => ({ timeline: { ...state.timeline, time } })),
  setSelectedStream: (path) => set(state => ({ timeline: { ...state.timeline, selectedStream: path } })),
  setPlaying: (playing) => set(state => ({
    timeline: {
      ...state.timeline,
      playing,
      mode: playing ? 'step' : state.timeline.mode,
    },
  })),
  setFps: (fps) => set(state => ({
    timeline: { ...state.timeline, fps: Math.max(1, Math.min(240, fps)) },
  })),

  runTree: EMPTY_TREE,
  setRunTree: (tree) => set({ runTree: tree }),
  selectedGroup: null,
  selectGroup: (path) => set({ selectedGroup: path, selectedRunId: null }),
  selectedDoc: null,
  selectDoc: (doc) => set({ selectedDoc: doc }),
  expandedGroups: new Set<string>(),
  toggleGroupExpanded: (path) => set(state => {
    const next = new Set(state.expandedGroups)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    return { expandedGroups: next }
  }),
  navigateNebo: (href) => {
    const link = parseNeboLink(href)
    if (!link) return
    if (link.kind === 'run') {
      if (!get().runs.has(link.runId)) {
        set({ notice: `Run ${link.runId} isn't loaded on this daemon.` })
        return
      }
      // Clearing selectedDoc matters when the link was clicked *inside*
      // the doc viewer — otherwise the run selects behind a doc that
      // stays on screen.
      set({ selectedRunId: link.runId, selectedGroup: null, selectedDoc: null })
      if (link.step !== null) {
        set(state => ({ timeline: { ...state.timeline, mode: 'step', step: link.step } }))
      }
      if (link.loggableId) {
        // Deterministic destination for loggable/stream links: always the
        // Flat view (every loggable has a card there, unlike the DAG),
        // then LoggableGridView switches to the owning tab, scrolls to the
        // card, and highlights it.
        set({
          viewMode: 'flat',
          pendingNavTarget: { loggableId: link.loggableId, name: link.name },
        })
      }
    } else {
      if (!(link.path in get().runTree.groups)) {
        set({ notice: `Group ${link.path} no longer exists.` })
        return
      }
      // No dedicated group view — reveal the group in the run tree by
      // expanding it and every ancestor.
      set(state => {
        const next = new Set(state.expandedGroups)
        const parts = link.path.split('/')
        for (let i = 1; i <= parts.length; i++) next.add(parts.slice(0, i).join('/'))
        return { expandedGroups: next }
      })
    }
  },
  notice: null,
  setNotice: (msg) => set({ notice: msg }),
  pendingNavTarget: null,
  setPendingNavTarget: (target) => set({ pendingNavTarget: target }),

  settings: initialSettings,

  labelKeySettings: loadLabelSettings(),
  registerLabelKey: (loggable, image, key) => set(state => {
    const k = `${loggable}|${image}|${key}`
    if (k in state.labelKeySettings) return state
    const next = { ...state.labelKeySettings, [k]: { visible: true, opacity: 70 } }
    saveLabelSettings(next)
    return { labelKeySettings: next }
  }),
  setLabelKeyVisible: (loggable, image, key, visible) => set(state => {
    const k = `${loggable}|${image}|${key}`
    const prev = state.labelKeySettings[k] ?? { visible: true, opacity: 70 }
    const next = { ...state.labelKeySettings, [k]: { ...prev, visible } }
    saveLabelSettings(next)
    return { labelKeySettings: next }
  }),
  setLabelKeyOpacity: (loggable, image, key, opacity) => set(state => {
    const k = `${loggable}|${image}|${key}`
    const prev = state.labelKeySettings[k] ?? { visible: true, opacity: 70 }
    const next = { ...state.labelKeySettings, [k]: { ...prev, opacity } }
    saveLabelSettings(next)
    return { labelKeySettings: next }
  }),

  // Every mutator below clones the run object (matching processWsEvents),
  // so `s.runs.get(id)` selectors fire on any change to that run — and
  // ONLY that run. Never mutate a stored run in place: components with
  // run-object selectors would silently miss the update.
  setRuns: (summaries, activeRunId) => set(state => {
    const runs = new Map(state.runs)
    for (const s of summaries) {
      const existing = runs.get(s.id)
      if (existing) {
        runs.set(s.id, { ...existing, summary: s })
      } else {
        runs.set(s.id, {
          summary: s,
          graph: null,
          texts: [],
          loggableMetrics: {},
          loggableImages: {},
          loggableAudio: {},
          loggableActions: {},
          bodyModels: {},
          alerts: [],
          loaded: false,
          globalLoggable: undefined,
        })
      }
    }
    // Auto-select the most recent run if none was ever selected — once.
    let selectedRunId = state.selectedRunId
    let autoSelected = state.autoSelected
    if (!selectedRunId && !autoSelected && summaries.length > 0) {
      selectedRunId = activeRunId ?? summaries[summaries.length - 1].id
      autoSelected = true
    }
    return { runs, activeRunId, selectedRunId, autoSelected }
  }),

  updateRunSummary: (summary) => set(state => {
    const runs = new Map(state.runs)
    const existing = runs.get(summary.id)
    if (existing) {
      runs.set(summary.id, { ...existing, summary })
    }
    return { runs }
  }),

  setRunGraph: (runId, graph) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) {
      runs.set(runId, { ...run, graph, loaded: true })
    }

    // Apply run-level UI defaults from nb.ui() exactly once per run so the
    // user's subsequent interactive changes are not clobbered on every
    // refetch. We touch only the fields the server explicitly provided.
    const patch: Partial<NeboStore> = { runs }
    const ui = graph.ui_config
    const alreadyApplied = state.appliedUiConfigRuns.has(runId)
    if (ui && !alreadyApplied) {
      const applied = new Set(state.appliedUiConfigRuns)
      applied.add(runId)
      patch.appliedUiConfigRuns = applied

      if (ui.layout === 'horizontal') {
        patch.dagDirection = 'LR'
      } else if (ui.layout === 'vertical') {
        patch.dagDirection = 'TB'
      }

      if (ui.view === 'dag') {
        patch.viewMode = 'graph'
      } else if (ui.view === 'flat') {
        patch.viewMode = 'flat'
      }

      if (ui.theme === 'dark' || ui.theme === 'light') {
        const nextSettings = { ...state.settings, theme: ui.theme }
        saveSettings(nextSettings)
        document.documentElement.classList.toggle('dark', ui.theme === 'dark')
        patch.settings = nextSettings
      }

      if (ui.tracker === 'time' || ui.tracker === 'step') {
        patch.timeline = { ...state.timeline, mode: ui.tracker }
      }

      if (ui.minimap === true || ui.minimap === false) {
        const base = patch.settings ?? state.settings
        const nextSettings = { ...base, showMinimap: ui.minimap }
        saveSettings(nextSettings)
        patch.settings = nextSettings
      }

      // Kick layout to re-run with the new direction.
      patch.layoutTrigger = state.layoutTrigger + 1
    }

    return patch
  }),

  setRunTexts: (runId, texts) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) runs.set(runId, { ...run, texts })
    return { runs }
  }),

  appendRunText: (runId, text) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) runs.set(runId, { ...run, texts: [...run.texts, text] })
    return { runs }
  }),

  setRunMetrics: (runId, metrics) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) runs.set(runId, { ...run, loggableMetrics: metrics })
    return { runs }
  }),

  setRunImages: (runId, images) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) {
      const merged: Record<string, ImageEntry[]> = { ...run.loggableImages }
      for (const [loggableId, entries] of Object.entries(images)) {
        const existing = merged[loggableId] ?? []
        const existingIds = new Set(existing.map(e => e.mediaId))
        const newEntries = entries.filter(e => !existingIds.has(e.mediaId))
        merged[loggableId] = [...existing, ...newEntries]
      }
      runs.set(runId, { ...run, loggableImages: merged })
    }
    return { runs }
  }),

  setRunAudio: (runId, audio) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) {
      const merged: Record<string, AudioEntry[]> = { ...run.loggableAudio }
      for (const [loggableId, entries] of Object.entries(audio)) {
        const existing = merged[loggableId] ?? []
        const existingIds = new Set(existing.map(e => e.mediaId))
        const newEntries = entries.filter(e => !existingIds.has(e.mediaId))
        merged[loggableId] = [...existing, ...newEntries]
      }
      runs.set(runId, { ...run, loggableAudio: merged })
    }
    return { runs }
  }),

  // Scene frames arrive twice: a decimated first paint, then the full
  // series (`replace`), matching how metrics hydrate. Merging by
  // (name, step, timestamp) keeps live WS frames that landed between the
  // two fetches instead of dropping them on the replace.
  setRunActions: (runId, actions, bodyModels, replace = false) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (!run) return { runs }
    const merged: Record<string, ActionFrame[]> = replace
      ? {}
      : { ...run.loggableActions }
    const key = (f: ActionFrame) => `${f.name}|${f.step}|${f.timestamp}`
    for (const [loggableId, frames] of Object.entries(actions)) {
      const existing = replace
        ? (run.loggableActions[loggableId] ?? [])
        : (merged[loggableId] ?? [])
      const incoming = new Set(frames.map(key))
      const kept = existing.filter(f => !incoming.has(key(f)))
      merged[loggableId] = replace
        ? [...frames, ...kept].sort(
            (a, b) => (a.step ?? 0) - (b.step ?? 0) || a.timestamp - b.timestamp,
          )
        : [...existing, ...frames.filter(f => !new Set(existing.map(key)).has(key(f)))]
    }
    runs.set(runId, {
      ...run,
      loggableActions: merged,
      bodyModels: { ...run.bodyModels, ...bodyModels },
    })
    return { runs }
  }),

  setRunAlerts: (runId, alerts) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) {
      // Merge with any WS-appended entries, keyed by (timestamp, title)
      // so the same firing seen on both paths dedupes to one.
      const key = (a: AlertEntry) => `${a.timestamp}|${a.title}`
      const seen = new Set(alerts.map(key))
      runs.set(runId, { ...run, alerts: [...alerts, ...run.alerts.filter(a => !seen.has(key(a)))] })
    }
    return { runs }
  }),

  appendMetric: (runId, loggableId, name, entry, type) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run) {
      const existing = run.loggableMetrics[loggableId]?.[name]
      const nextSeries: LoggableMetricSeries =
        !existing
          ? { type, entries: [entry] }
          : type === 'line' || type === 'scatter'
            ? { ...existing, entries: [...existing.entries, entry] }
            // Bar / pie / histogram are snapshots — re-emitting the same
            // name replaces the prior value rather than stacking another
            // entry, mirroring the daemon's persistence model.
            : { ...existing, entries: [entry] }
      runs.set(runId, {
        ...run,
        loggableMetrics: {
          ...run.loggableMetrics,
          [loggableId]: { ...(run.loggableMetrics[loggableId] ?? {}), [name]: nextSeries },
        },
      })
    }
    return { runs }
  }),

  updateNodeProgress: (runId, nodeId, progress) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run?.graph?.nodes[nodeId]) {
      runs.set(runId, {
        ...run,
        graph: {
          ...run.graph,
          nodes: {
            ...run.graph.nodes,
            [nodeId]: { ...run.graph.nodes[nodeId], progress },
          },
        },
      })
    }
    return { runs }
  }),

  incrementNodeExecCount: (runId, loggableId) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run?.graph?.nodes[loggableId]) {
      runs.set(runId, {
        ...run,
        graph: {
          ...run.graph,
          nodes: {
            ...run.graph.nodes,
            [loggableId]: {
              ...run.graph.nodes[loggableId],
              exec_count: run.graph.nodes[loggableId].exec_count + 1,
            },
          },
        },
      })
    }
    return { runs }
  }),

  addEdge: (runId, source, target) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run?.graph) {
      const exists = run.graph.edges.some(e => e.source === source && e.target === target)
      if (!exists) {
        const graph = {
          ...run.graph,
          edges: [...run.graph.edges, { source, target }],
          nodes: {
            ...run.graph.nodes,
            ...(run.graph.nodes[target] ? {
              [target]: { ...run.graph.nodes[target], is_source: false },
            } : {}),
          },
        }
        runs.set(runId, {
          ...run,
          graph,
          summary: { ...run.summary, edge_count: graph.edges.length },
        })
      }
    }
    return { runs }
  }),

  setWorkflowDescription: (runId, description) => set(state => {
    const runs = new Map(state.runs)
    const run = runs.get(runId)
    if (run?.graph) {
      runs.set(runId, { ...run, graph: { ...run.graph, workflow_description: description } })
    }
    return { runs }
  }),

  selectRun: (runId) => set({ selectedRunId: runId, selectedGroup: null, selectedDoc: null }),
  requestLayout: () => set(state => ({ layoutTrigger: state.layoutTrigger + 1 })),

  updateNodePosition: (runId, nodeId, pos) => set(state => {
    const positions = new Map(state.nodePositions)
    if (!positions.has(runId)) positions.set(runId, new Map())
    positions.get(runId)!.set(nodeId, pos)
    return { nodePositions: positions }
  }),

  resetLayout: (runId) => set(state => {
    const positions = new Map(state.nodePositions)
    positions.delete(runId)
    return { nodePositions: positions }
  }),

  updateSetting: (key, value) => set(state => {
    const next = { ...state.settings, [key]: value }
    saveSettings(next)
    if (key === 'theme') {
      document.documentElement.classList.toggle('dark', next.theme === 'dark')
    }
    return { settings: next }
  }),

  processWsEvents: (runId, events) => {
    // Single batched set() — avoids N re-renders per WS batch
    set(state => {
      const runs = new Map(state.runs)

      // Ensure the run exists
      let run = runs.get(runId)
      if (!run) {
        run = {
          summary: {
            id: runId,
            script_path: 'direct',
            args: [],
            started_at: new Date().toISOString(),
            last_event_at: Date.now() / 1000,
            node_count: 0,
            edge_count: 0,
            text_count: 0,
            run_name: null,
          },
          graph: null,
          texts: [],
          loggableMetrics: {},
          loggableImages: {},
          loggableAudio: {},
          loggableActions: {},
          bodyModels: {},
          alerts: [],
          loaded: false,
          globalLoggable: undefined,
        }
      }
      // Clone run so selectors see a new reference
      run = { ...run } as RunState
      // Any event in this batch means the run is active now — refresh
      // recency so the "live" accent stays lit while events stream (it
      // decays LIVE_RECENCY_S after the last one). There is no ended_at.
      run.summary = { ...run.summary, last_event_at: Date.now() / 1000 }
      runs.set(runId, run)

      // Accumulate new text entries so we can spread once at the end
      const newTexts: TextEntry[] = []

      // Accumulating-metric appends are pooled per (loggable, name) and
      // applied in ONE spread per series after the loop — N plain metric
      // events in a batch cost O(n + N), not N × O(n). Snapshot types
      // (bar/pie/histogram) still overwrite inline.
      const pendingMetrics = new Map<string, {
        lid: string; name: string; mtype: MetricType; entries: MetricEntry[]
      }>()
      const pushMetricEntries = (lid: string, name: string, mtype: MetricType, entries: MetricEntry[]) => {
        const key = `${lid} ${name}`
        const pending = pendingMetrics.get(key)
        if (pending) pending.entries.push(...entries)
        else pendingMetrics.set(key, { lid, name, mtype, entries })
      }

      for (const event of events) {
        const etype = event.type
        const loggableId = event.loggable_id as string | undefined
        const data = (event.data ?? event) as Record<string, unknown>

        switch (etype) {
          case 'text':
          case 'log':  // legacy spelling from a mixed-version daemon
            newTexts.push({
              timestamp: (event.timestamp as number) ?? Date.now() / 1000,
              node: loggableId ?? null,
              name: (event.name as string) ?? (data.name as string) ?? 'text',
              message: (event.message as string) ?? (data.message as string) ?? '',
              step: (event.step as number) ?? null,
            })
            break

          case 'metric': {
            const lid = event.loggable_id as string | undefined
            if (!lid) break
            const mname = (event.name as string) ?? (data.name as string) ?? ''
            const mtype = ((event.metric_type as MetricType) ?? (data.metric_type as MetricType)) ?? 'line'
            const entry: MetricEntry = {
              step: (event.step as number | null) ?? (data.step as number | null) ?? null,
              value: event.value ?? data.value,
              tags: ((event.tags as string[]) ?? (data.tags as string[]) ?? []),
              timestamp: (event.timestamp as number) ?? Date.now() / 1000,
            }
            const colorsFlag = (event.colors as boolean | undefined) ?? (data.colors as boolean | undefined)
            if (colorsFlag !== undefined) entry.colors = colorsFlag
            // Line and scatter accumulate — pooled and applied once after
            // the loop. Bar / pie / histogram are snapshots that overwrite
            // prior emissions immediately (immutable update so
            // `useMemo([series.entries])` downstream fires). This matches
            // the daemon's `nebo/server/daemon.py::_process_event`.
            const accumulates = mtype === 'line' || mtype === 'scatter'
            if (accumulates) {
              pushMetricEntries(lid, mname, mtype, [entry])
              break
            }
            const existing = run.loggableMetrics[lid]?.[mname]
            const nextSeries: LoggableMetricSeries = existing
              ? { ...existing, entries: [entry] }
              : { type: mtype, entries: [entry] }
            run.loggableMetrics = {
              ...run.loggableMetrics,
              [lid]: { ...(run.loggableMetrics[lid] ?? {}), [mname]: nextSeries },
            }
            break
          }

          case 'metric_batch': {
            // Columnar batch of accumulating-metric points (format v4):
            // parallel steps/timestamps/values arrays with whole-batch
            // tags/colors. Routed through the same per-series pending pool
            // as plain metric events so ordering is uniform and each
            // series costs one spread per WS batch.
            const lid = event.loggable_id as string | undefined
            if (!lid) break
            const mname = (event.name as string) ?? ''
            const mtype = (event.metric_type as MetricType) ?? 'line'
            const steps = (event.steps as (number | null)[]) ?? []
            const timestamps = (event.timestamps as number[]) ?? []
            const values = (event.values as unknown[]) ?? []
            const tags = (event.tags as string[]) ?? []
            const colorsFlag = event.colors as boolean | undefined
            const batchEntries: MetricEntry[] = steps.map((step, i) => {
              const entry: MetricEntry = {
                step: step ?? null,
                value: values[i],
                tags,
                timestamp: timestamps[i],
              }
              if (colorsFlag !== undefined) entry.colors = colorsFlag
              return entry
            })
            if (batchEntries.length === 0) break
            pushMetricEntries(lid, mname, mtype, batchEntries)
            break
          }

          case 'progress':
            if (loggableId && run.graph?.nodes[loggableId]) {
              // New node object so primitive selectors detect the change; graph ref stays stable
              run.graph.nodes[loggableId] = {
                ...run.graph.nodes[loggableId],
                progress: data as { current: number; total: number; name?: string },
              }
            }
            break

          case 'loggable_register': {
            const lid = (data.loggable_id as string) || ''
            const kind = (data.kind as 'node' | 'global' | 'agent') ?? 'node'
            if (kind === 'global') {
              // Globals are not DAG nodes — track separately, do not insert into graph.nodes
              if (!run.globalLoggable) {
                run.globalLoggable = { loggableId: lid, kind: 'global' }
              }
            } else if (kind === 'agent') {
              // Agent sandbox loggable for MCP-authored entries — also non-DAG.
              if (!run.agentLoggable) {
                run.agentLoggable = { loggableId: lid, kind: 'agent' }
              }
            } else {
              if (!run.graph) {
                run.graph = { nodes: {}, edges: [], workflow_description: null }
              }
              if (!run.graph.nodes[lid]) {
                // Structural change — new graph object
                run.graph = {
                  ...run.graph,
                  nodes: {
                    ...run.graph.nodes,
                    [lid]: {
                      name: lid,
                      func_name: (data.func_name as string) || '',
                      docstring: (data.docstring as string) || null,
                      exec_count: 0,
                      is_source: true,
                      params: {},
                      progress: null,
                      group: (data.group as string) || null,
                      ui_hints: (data.ui_hints as Record<string, unknown>) || null,
                    },
                  },
                }
                run.summary.node_count = Object.keys(run.graph.nodes).length
              }
            }
            break
          }

          case 'node_executed': {
            const nid = (data.loggable_id as string) ?? loggableId ?? ''
            // Transport-coalesced execution ticks carry a count delta.
            const count = (data.count as number | undefined) ?? 1
            if (nid && run.graph?.nodes[nid]) {
              // New node object for selector detection; graph ref stays stable
              run.graph.nodes[nid] = {
                ...run.graph.nodes[nid],
                exec_count: run.graph.nodes[nid].exec_count + count,
              }
            }
            const caller = data.caller as string | undefined
            if (caller && nid && run.graph) {
              const exists = run.graph.edges.some(e => e.source === caller && e.target === nid)
              if (!exists) {
                // Structural change — new graph object
                run.graph = {
                  ...run.graph,
                  edges: [...run.graph.edges, { source: caller, target: nid }],
                  nodes: {
                    ...run.graph.nodes,
                    ...(run.graph.nodes[nid] ? {
                      [nid]: { ...run.graph.nodes[nid], is_source: false },
                    } : {}),
                  },
                }
                run.summary.edge_count = run.graph.edges.length
              }
            }
            break
          }

          case 'edge': {
            const source = (data.source as string) ?? ''
            const target = (data.target as string) ?? ''
            if (run.graph) {
              const exists = run.graph.edges.some(e => e.source === source && e.target === target)
              if (!exists) {
                run.graph = {
                  ...run.graph,
                  edges: [...run.graph.edges, { source, target }],
                  nodes: {
                    ...run.graph.nodes,
                    ...(run.graph.nodes[target] ? {
                      [target]: { ...run.graph.nodes[target], is_source: false },
                    } : {}),
                  },
                }
                run.summary.edge_count = run.graph.edges.length
              }
            }
            break
          }

          case 'image':
            if (loggableId) {
              const ev = event as Record<string, unknown>
              const prev = run.loggableImages[loggableId] ?? []
              run.loggableImages = { ...run.loggableImages, [loggableId]: [...prev, {
                node: loggableId,
                mediaId: (ev.media_id as string) ?? '',
                name: (ev.name as string) ?? '',
                step: (ev.step as number) ?? null,
                timestamp: (ev.timestamp as number) ?? Date.now() / 1000,
                labels: (ev.labels as LabelsPayload | null | undefined) ?? null,
              }] }
            }
            break

          case 'audio':
            if (loggableId) {
              const ev = event as Record<string, unknown>
              const prev = run.loggableAudio[loggableId] ?? []
              run.loggableAudio = { ...run.loggableAudio, [loggableId]: [...prev, {
                node: loggableId,
                mediaId: (ev.media_id as string) ?? '',
                name: (ev.name as string) ?? '',
                sr: (ev.sr as number) ?? 16000,
                step: (ev.step as number) ?? null,
                timestamp: (ev.timestamp as number) ?? Date.now() / 1000,
              }] }
            }
            break

          case 'body_model': {
            const ev = event as Record<string, unknown>
            const modelId = (ev.model_id as string) ?? ''
            if (modelId) {
              run.bodyModels = { ...run.bodyModels, [modelId]: {
                model_id: modelId,
                name: (ev.name as string) ?? '',
                media_id: (ev.media_id as string) ?? '',
                body_names: (ev.body_names as string[]) ?? [],
                source_format: (ev.source_format as string) ?? '',
              } }
            }
            break
          }

          case 'body_transform':
            if (loggableId) {
              const ev = event as Record<string, unknown>
              const prev = run.loggableActions[loggableId] ?? []
              run.loggableActions = { ...run.loggableActions, [loggableId]: [...prev, {
                node: loggableId,
                name: (ev.name as string) ?? '',
                step: (ev.step as number) ?? null,
                timestamp: (ev.timestamp as number) ?? Date.now() / 1000,
                instances: (ev.instances as ActionFrame['instances']) ?? {},
              }] }
            }
            break

          case 'config':
            if (loggableId && run.graph?.nodes[loggableId]) {
              run.graph.nodes[loggableId] = {
                ...run.graph.nodes[loggableId],
                params: { ...run.graph.nodes[loggableId].params, ...(data as Record<string, unknown>) },
              }
            }
            break

          case 'description':
            if (run.graph) {
              run.graph = { ...run.graph, workflow_description: (data.description as string) ?? '' }
            }
            break

          case 'run_start': {
            const scriptPath = (data.script_path as string) ?? ''
            const runName = (data.run_name as string) ?? null
            const patch: Partial<typeof run.summary> = {}
            if (scriptPath) patch.script_path = scriptPath
            if (runName !== null) patch.run_name = runName
            run.summary = { ...run.summary, ...patch }
            break
          }

          case 'alert': {
            run.alerts = [...run.alerts, {
              title: (data.title as string) ?? '',
              text: (data.text as string) ?? '',
              level: Number(data.level ?? 20),
              level_name: (data.level_name as string) ?? '',
              triggered_by: (data.triggered_by as string) ?? 'code',
              loggable_id: (event.loggable_id as string | undefined) ?? (data.loggable_id as string | null | undefined) ?? null,
              timestamp: (data.timestamp as number) ?? (event.timestamp as number) ?? Date.now() / 1000,
            }]
            break
          }

          case 'run_config': {
            if (run.graph) {
              run.graph = { ...run.graph, run_config: data as Record<string, unknown> }
            }
            break
          }

          // run_completed carries no read-side meaning — it's a writer
          // finalization marker. Recency is refreshed for the batch above.
        }
      }

      // Batch-append accumulated text entries
      if (newTexts.length > 0) {
        run.texts = [...run.texts, ...newTexts]
      }

      // Apply pooled accumulating-metric appends: one spread per series.
      for (const p of pendingMetrics.values()) {
        const existing = run.loggableMetrics[p.lid]?.[p.name]
        const nextSeries: LoggableMetricSeries = existing
          ? { ...existing, entries: [...existing.entries, ...p.entries] }
          : { type: p.mtype, entries: p.entries }
        run.loggableMetrics = {
          ...run.loggableMetrics,
          [p.lid]: { ...(run.loggableMetrics[p.lid] ?? {}), [p.name]: nextSeries },
        }
      }

      return { runs }
    })
  },
}))

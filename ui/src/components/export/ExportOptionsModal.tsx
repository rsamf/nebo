import { useCallback, useEffect, useMemo, useState } from 'react'
import { useReactFlow } from '@xyflow/react'
import { Modal } from '@/components/ui/modal'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useStore } from '@/store'
import { cn } from '@/lib/utils'
import { runExport } from './runExport'
import {
  DEFAULT_EXPORT_OPTIONS,
  type ConfigStyle,
  type ExportFormat,
  type ExportOptions,
  type Padding,
  type TabbedContent,
  type Theme,
} from './types'
import type { NodePos } from './DiagramExportRoot'

const STORAGE_KEY = 'nebo_export_options'

function loadOptions(): ExportOptions {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return { ...DEFAULT_EXPORT_OPTIONS, ...JSON.parse(raw) }
  } catch { /* ignore */ }
  return { ...DEFAULT_EXPORT_OPTIONS }
}

function saveOptions(opts: ExportOptions) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(opts))
  } catch { /* ignore */ }
}

interface ExportOptionsModalProps {
  open: boolean
  onClose: () => void
  runId: string
}

export function ExportOptionsModal({ open, onClose, runId }: ExportOptionsModalProps) {
  const [options, setOptions] = useState<ExportOptions>(() => loadOptions())
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { getNodes } = useReactFlow()
  const graph = useStore(s => s.runs.get(runId)?.graph)
  const nodeCount = graph ? Object.keys(graph.nodes).length : 0

  useEffect(() => {
    if (open) {
      setOptions(loadOptions())
      setError(null)
    }
  }, [open])

  const update = useCallback(<K extends keyof ExportOptions>(key: K, value: ExportOptions[K]) => {
    setOptions(prev => {
      const next = { ...prev, [key]: value }
      saveOptions(next)
      return next
    })
  }, [])

  const livePositions = useMemo(() => {
    if (!open) return undefined
    const map = new Map<string, NodePos>()
    for (const n of getNodes()) {
      if (n.type !== 'nebo') continue
      map.set(n.id, {
        x: n.position.x,
        y: n.position.y,
        width: n.measured?.width ?? 360,
        height: n.measured?.height ?? 200,
      })
    }
    return map
    // Snapshot once per open — don't re-evaluate while the user toggles options.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const isFormatRaster = options.format === 'png' || options.format === 'jpg' || options.format === 'pdf'
  const isFormatSvg = options.format === 'svg'
  const isFormatDrawio = options.format === 'drawio'
  const transparentDisabled = !isFormatRaster || options.format === 'jpg'
  const resolutionDisabled = !isFormatRaster

  const handleExport = useCallback(async () => {
    setExporting(true)
    setError(null)
    try {
      await runExport(options, runId, livePositions)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed.')
    } finally {
      setExporting(false)
    }
  }, [options, runId, livePositions, onClose])

  return (
    <Modal open={open} onClose={onClose} title="Export DAG" widthClass="max-w-3xl">
      <div className="space-y-5">
        {/* Format picker */}
        <Section label="Format">
          <Segmented
            value={options.format}
            options={[
              { value: 'png', label: 'PNG' },
              { value: 'jpg', label: 'JPG' },
              { value: 'pdf', label: 'PDF' },
              { value: 'svg', label: 'SVG' },
              { value: 'drawio', label: '.drawio' },
            ]}
            onChange={v => update('format', v as ExportFormat)}
          />
        </Section>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-5">
          {/* Layout & content */}
          <div className="space-y-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Layout & content
            </h4>
            <Row label="Auto-layout" hint="Re-run dagre to fit the simplified node footprints.">
              <Switch
                checked={options.autoLayout}
                onCheckedChange={v => update('autoLayout', v)}
              />
            </Row>
            <Row label="Config style">
              <Segmented
                value={options.configStyle}
                options={[
                  { value: 'none', label: 'None' },
                  { value: 'chips', label: 'Chips' },
                  { value: 'complete', label: 'Complete' },
                ]}
                onChange={v => update('configStyle', v as ConfigStyle)}
              />
            </Row>
            <Row label="Show exec count">
              <Switch
                checked={options.showExecCount}
                onCheckedChange={v => update('showExecCount', v)}
              />
            </Row>
            <Row label="Show full docstring">
              <Switch
                checked={options.showDocstring}
                onCheckedChange={v => update('showDocstring', v)}
              />
            </Row>
            <Row label="Tabbed content">
              <Segmented
                value={options.tabbedContent}
                options={[
                  { value: 'none', label: 'None' },
                  { value: 'active', label: 'Active' },
                ]}
                onChange={v => update('tabbedContent', v as TabbedContent)}
              />
            </Row>
            <Row
              label="Entries per node"
              hint="Cap on text / images / charts / audio shown in each node."
              disabled={options.tabbedContent === 'none'}
            >
              <input
                type="number"
                min={1}
                max={9999}
                value={options.entriesPerNode}
                disabled={options.tabbedContent === 'none'}
                onChange={e => {
                  const n = parseInt(e.target.value, 10)
                  if (!Number.isNaN(n) && n >= 1) update('entriesPerNode', n)
                }}
                className="w-16 h-8 px-2 text-xs rounded-md border border-input bg-background text-foreground"
              />
            </Row>
            <Row label="Show color hints" hint="Honor @nb.fn(ui={color}) borders.">
              <Switch
                checked={options.showColorHints}
                onCheckedChange={v => update('showColorHints', v)}
              />
            </Row>
          </div>

          {/* Output */}
          <div className="space-y-4">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Output
            </h4>
            <Row label="Theme">
              <Segmented
                value={options.theme}
                options={[
                  { value: 'dark', label: 'Dark' },
                  { value: 'light', label: 'Light' },
                ]}
                onChange={v => update('theme', v as Theme)}
              />
            </Row>
            <Row label="Padding">
              <Segmented
                value={options.padding}
                options={[
                  { value: 'tight', label: 'Tight' },
                  { value: 'comfortable', label: 'Comfortable' },
                ]}
                onChange={v => update('padding', v as Padding)}
              />
            </Row>
            <Row
              label="Transparent background"
              hint={
                isFormatSvg
                  ? 'SVG output has no background by default.'
                  : isFormatDrawio
                  ? 'Drawio cells use the theme fill color.'
                  : options.format === 'jpg'
                  ? 'JPG has no alpha channel.'
                  : undefined
              }
              disabled={transparentDisabled}
            >
              <Switch
                checked={options.transparentBackground && !transparentDisabled}
                disabled={transparentDisabled}
                onCheckedChange={v => update('transparentBackground', v)}
              />
            </Row>
            <Row
              label="Resolution"
              hint={resolutionDisabled ? 'Vector formats are resolution-independent.' : undefined}
              disabled={resolutionDisabled}
            >
              <Segmented
                value={String(options.pixelRatio)}
                disabled={resolutionDisabled}
                options={[
                  { value: '1', label: '1x' },
                  { value: '2', label: '2x' },
                  { value: '3', label: '3x' },
                  { value: '4', label: '4x' },
                ]}
                onChange={v => update('pixelRatio', Number(v) as ExportOptions['pixelRatio'])}
              />
            </Row>
          </div>
        </div>

        {error && (
          <div className="text-xs text-red-400 bg-red-500/10 border border-red-500/30 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex items-center justify-end gap-2 pt-3 border-t border-border">
          {nodeCount === 0 && (
            <span className="text-xs text-muted-foreground mr-auto">Nothing to export.</span>
          )}
          <Button variant="ghost" onClick={onClose} disabled={exporting}>Cancel</Button>
          <Button
            onClick={handleExport}
            disabled={exporting || nodeCount === 0}
          >
            {exporting ? 'Exporting…' : 'Export'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

// ─── Tiny layout helpers ──────────────────────────────────────────────────

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </h4>
      <div>{children}</div>
    </div>
  )
}

function Row({
  label,
  hint,
  disabled,
  children,
}: {
  label: string
  hint?: string
  disabled?: boolean
  children: React.ReactNode
}) {
  return (
    <div className={cn('flex items-start justify-between gap-4', disabled && 'opacity-50')}>
      <div className="flex-1 min-w-0">
        <div className="text-sm">{label}</div>
        {hint && <div className="text-[11px] text-muted-foreground mt-0.5">{hint}</div>}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  )
}

function Segmented<T extends string>({
  value,
  options,
  onChange,
  disabled,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (value: T) => void
  disabled?: boolean
}) {
  return (
    <div className="inline-flex rounded-md border border-input overflow-hidden">
      {options.map(opt => {
        const active = opt.value === value
        return (
          <button
            key={opt.value}
            type="button"
            disabled={disabled}
            aria-pressed={active}
            onClick={() => onChange(opt.value)}
            className={cn(
              'px-2.5 py-1 text-xs font-medium transition-colors',
              'border-r last:border-r-0 border-input',
              active
                ? 'bg-primary text-primary-foreground'
                : 'bg-background text-muted-foreground hover:bg-accent hover:text-accent-foreground',
              disabled && 'cursor-not-allowed opacity-50',
            )}
          >
            {opt.label}
          </button>
        )
      })}
    </div>
  )
}

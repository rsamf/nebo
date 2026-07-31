import { memo, useRef, useState } from 'react'
import { useStore } from '@/store'
import { RightPanelSettings } from './RightPanelSettings'
import { NeboMarkdown } from '@/components/shared/NeboMarkdown'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { FileText, Settings2, SlidersHorizontal } from 'lucide-react'
import { cn } from '@/lib/utils'

// Icon-only tabs; the label lives in a tooltip.
const TABS = [
  { value: 'markdown', label: 'Markdown', Icon: FileText },
  { value: 'config', label: 'Config', Icon: SlidersHorizontal },
  { value: 'settings', label: 'Settings', Icon: Settings2 },
] as const

const WIDTH_KEY = 'nebo_right_panel_width'
const MIN_W = 240
const MAX_W = 640

function loadWidth(): number {
  const v = Number(localStorage.getItem(WIDTH_KEY))
  return Number.isFinite(v) && v >= MIN_W ? Math.min(v, MAX_W) : 320
}

/**
 * Recursive structured view of a run's config: nested objects indent as
 * labeled groups, arrays index their items, primitives render as mono
 * values. Replaces the old flat chips.
 */
function ConfigNode({ value, depth }: { value: unknown; depth: number }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="font-mono text-muted-foreground">[]</span>
    return (
      <div className="space-y-0.5">
        {value.map((v, i) => (
          <div key={i} className="flex gap-2" style={{ paddingLeft: depth > 0 ? 12 : 0 }}>
            <span className="shrink-0 font-mono text-muted-foreground">[{i}]</span>
            <ConfigNode value={v} depth={depth + 1} />
          </div>
        ))}
      </div>
    )
  }
  if (typeof value === 'object' && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>)
    if (entries.length === 0) return <span className="font-mono text-muted-foreground">{'{}'}</span>
    return (
      <div className="space-y-0.5">
        {entries.map(([k, v]) => {
          const nested = typeof v === 'object' && v !== null
          return (
            <div key={k} style={{ paddingLeft: depth > 0 ? 12 : 0 }}>
              {nested ? (
                <>
                  <div className="font-mono text-muted-foreground">{k}:</div>
                  <ConfigNode value={v} depth={depth + 1} />
                </>
              ) : (
                <div className="flex gap-2">
                  <span className="shrink-0 font-mono text-muted-foreground">{k}:</span>
                  <ConfigNode value={v} depth={depth + 1} />
                </div>
              )}
            </div>
          )
        })}
      </div>
    )
  }
  return (
    <span className="break-all font-mono text-foreground">
      {typeof value === 'string' ? value : String(value)}
    </span>
  )
}

function MarkdownTab({ description }: { description: string | null | undefined }) {
  if (!description) {
    return (
      <p className="p-3 text-xs text-muted-foreground">
        No notes — add them with <code className="rounded bg-muted px-1">nb.md(…)</code>
      </p>
    )
  }
  return (
    <div className={cn(
      'px-3 py-2 prose prose-sm dark:prose-invert max-w-none',
      'prose-headings:mt-3 prose-headings:mb-1.5',
      '[&_h1]:!text-sm [&_h1]:!font-semibold [&_h2]:!text-xs [&_h2]:!font-semibold [&_h3]:!text-xs',
      'prose-p:text-xs prose-p:leading-relaxed prose-p:my-1.5',
      'prose-li:text-xs prose-li:my-0.5',
      'prose-code:text-xs prose-code:bg-muted prose-code:px-1 prose-code:py-0.5 prose-code:rounded',
      'prose-pre:bg-muted prose-pre:text-xs prose-pre:my-2',
    )}>
      <NeboMarkdown>{description}</NeboMarkdown>
    </div>
  )
}

function ConfigTab({ config }: { config: Record<string, unknown> | null }) {
  if (!config || Object.keys(config).length === 0) {
    return (
      <p className="p-3 text-xs text-muted-foreground">
        No config — log one with <code className="rounded bg-muted px-1">nb.log_cfg(…)</code>
      </p>
    )
  }
  return (
    <div className="px-3 py-2 text-xs">
      <ConfigNode value={config} depth={0} />
    </div>
  )
}

/** Right side panel: the run's markdown, structured config, and the chart
 *  settings, in tabs. Resizable by dragging its left edge. */
export const RightPanel = memo(function RightPanel({ runId }: { runId: string }) {
  const description = useStore(s => s.runs.get(runId)?.graph?.workflow_description)
  const config = useStore(s => {
    const run = s.runs.get(runId)
    return run?.graph?.run_config ?? run?.summary.run_config ?? null
  })

  const [width, setWidth] = useState(loadWidth)
  const widthRef = useRef(width)
  const dragging = useRef(false)

  const onHandleDown = (e: React.PointerEvent) => {
    dragging.current = true
    ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
  }
  const onHandleMove = (e: React.PointerEvent) => {
    if (!dragging.current) return
    const w = Math.max(MIN_W, Math.min(MAX_W, window.innerWidth - e.clientX))
    widthRef.current = w
    setWidth(w)
  }
  const onHandleUp = () => {
    if (!dragging.current) return
    dragging.current = false
    localStorage.setItem(WIDTH_KEY, String(widthRef.current))
  }

  return (
    <div className="relative flex h-full shrink-0 flex-col border-l border-border" style={{ width }}>
      {/* drag-to-resize handle on the left edge */}
      <div
        className="absolute inset-y-0 left-0 z-10 w-1 cursor-ew-resize hover:bg-primary/40"
        onPointerDown={onHandleDown}
        onPointerMove={onHandleMove}
        onPointerUp={onHandleUp}
      />
      <Tabs defaultValue="markdown" className="flex h-full min-h-0 flex-col">
        <TabsList className="w-full justify-start rounded-none border-b border-border bg-transparent px-2 shrink-0">
          {TABS.map(({ value, label, Icon }) => (
            <Tooltip key={value}>
              <TooltipTrigger asChild>
                <TabsTrigger
                  value={value}
                  aria-label={label}
                  className="h-7 w-9 px-0 data-[state=active]:bg-muted"
                >
                  <Icon className="h-4 w-4" />
                </TabsTrigger>
              </TooltipTrigger>
              <TooltipContent side="bottom">{label}</TooltipContent>
            </Tooltip>
          ))}
        </TabsList>
        <TabsContent value="markdown" className="mt-0 min-h-0 flex-1">
          <ScrollArea className="h-full">
            <MarkdownTab description={description} />
          </ScrollArea>
        </TabsContent>
        <TabsContent value="config" className="mt-0 min-h-0 flex-1">
          <ScrollArea className="h-full">
            <ConfigTab config={config as Record<string, unknown> | null} />
          </ScrollArea>
        </TabsContent>
        <TabsContent value="settings" className="mt-0 min-h-0 flex-1 overflow-hidden">
          <RightPanelSettings />
        </TabsContent>
      </Tabs>
    </div>
  )
})

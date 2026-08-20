import { useCallback, useEffect } from 'react'
import { useStore } from '@/store'
import { useIsDesktop } from '@/hooks/useMediaQuery'
import { usePlayback } from '@/hooks/usePlayback'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { ChevronLeft, ChevronRight, Maximize, ChevronDown, ChevronUp, SlidersHorizontal, Play, Pause } from 'lucide-react'
import { MODALITY_COLORS, STREAM_MODALITIES, type StreamModality } from '@/lib/streams'

const MODALITY_LABELS: Record<StreamModality, string> = {
  text: 'Text', image: 'Images', audio: 'Audio', action: 'Actions',
}
const MODALITIES = STREAM_MODALITIES

// Modality toggle chips. Desktop renders them in the tree column under the
// stream search field (hidden while the tracker is collapsed); mobile puts
// them in the Filters popover.
export function ModalityChips({ activeModalities, onToggleModality }: {
  activeModalities: Set<StreamModality>
  onToggleModality: (m: StreamModality) => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-0.5">
      {MODALITIES.map(m => {
        const active = activeModalities.has(m)
        return (
          <Badge
            key={m}
            variant={active ? 'default' : 'outline'}
            className="cursor-pointer select-none gap-0.5 px-1.5 py-0 text-[9px]"
            style={active ? { backgroundColor: MODALITY_COLORS[m], borderColor: MODALITY_COLORS[m] } : undefined}
            onClick={() => onToggleModality(m)}
          >
            <span className="inline-block h-1 w-1 rounded-full" style={{ backgroundColor: active ? '#fff' : MODALITY_COLORS[m] }} />
            {MODALITY_LABELS[m]}
          </Badge>
        )
      })}
    </div>
  )
}

interface Props {
  minStep: number
  maxStep: number
  hasSteps: boolean
  activeModalities: Set<StreamModality>
  onToggleModality: (m: StreamModality) => void
  onResetZoom: () => void
  onClearFilters: () => void
  query: string
  onQueryChange: (q: string) => void
  collapsed: boolean
  onToggleCollapse: () => void
}

export function TrackerControls({ minStep, maxStep, hasSteps, activeModalities, onToggleModality, onResetZoom, onClearFilters, query, onQueryChange, collapsed, onToggleCollapse }: Props) {
  const timeline = useStore(s => s.timeline)
  const setMode = useStore(s => s.setTimelineMode)
  const setStep = useStore(s => s.setTimelineStep)
  const selectStep = useStore(s => s.selectTimelineStep)
  const setPlaying = useStore(s => s.setPlaying)
  const setFps = useStore(s => s.setFps)
  const isStep = timeline.mode === 'step'
  const isDesktop = useIsDesktop()

  // Playback owns the shared playhead; the tracker is where it belongs
  // since every panel follows that playhead.
  usePlayback(minStep, maxStep, hasSteps)

  // Stepping from Time mode flips to Step mode, exactly as clicking a
  // chart datapoint does — otherwise the arrows would set a step the
  // tracker isn't currently displaying.
  const stepBy = useCallback((d: number) => {
    if (!hasSteps) return
    const cur = timeline.step ?? minStep
    selectStep(Math.max(minStep, Math.min(maxStep, cur + d)))
  }, [hasSteps, timeline.step, minStep, maxStep, selectStep])

  // Ctrl/⌘ + Left/Right steps the playhead (skips when typing in a field).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (!e.ctrlKey && !e.metaKey) return
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
      const tag = (e.target as HTMLElement)?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (!hasSteps) return
      e.preventDefault()
      stepBy(e.key === 'ArrowRight' ? 1 : -1)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [hasSteps, stepBy])

  const modeSelect = (triggerClass: string) => (
    <Select value={timeline.mode} onValueChange={(v) => setMode(v as 'time' | 'step')}>
      <SelectTrigger className={triggerClass}><SelectValue /></SelectTrigger>
      <SelectContent>
        <SelectItem value="step">Step</SelectItem>
        <SelectItem value="time">Time</SelectItem>
      </SelectContent>
    </Select>
  )

  return (
    <div className="flex items-center gap-2 border-b border-border bg-background px-2 py-1.5 shrink-0">
      {isDesktop ? (
        modeSelect('h-7 w-[88px] text-xs')
      ) : (
        // Mobile: fold the filter controls into a single menu popover.
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" className="h-7 gap-1.5 px-2 text-xs">
              <SlidersHorizontal size={14} /> Filters
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-60 space-y-3">
            <div className="space-y-1.5">
              <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Search</div>
              <Input placeholder="Search streams…" value={query} onChange={e => onQueryChange(e.target.value)} className="h-7 text-xs" />
            </div>
            <div className="space-y-1.5">
              <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Modalities</div>
              <ModalityChips activeModalities={activeModalities} onToggleModality={onToggleModality} />
            </div>
            <div className="space-y-1.5">
              <div className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Axis</div>
              {modeSelect('h-7 w-full text-xs')}
            </div>
            <div className="flex flex-col gap-1.5 pt-1">
              <Button variant="outline" className="h-7 justify-start gap-2 text-xs" onClick={onResetZoom}>
                <Maximize size={14} /> Reset zoom
              </Button>
              <Button variant="outline" className="h-7 justify-start text-xs" onClick={onClearFilters}>
                Clear all filters
              </Button>
            </div>
          </PopoverContent>
        </Popover>
      )}

      {/* Step navigation + playback stay in the bar on both layouts. */}
      <div className="flex items-center gap-0.5">
        <Button variant="ghost" className="h-7 w-7 p-0" disabled={!hasSteps} title="Previous step" onClick={() => stepBy(-1)}>
          <ChevronLeft size={15} />
        </Button>
        <Button
          variant="ghost"
          className="h-7 w-7 p-0"
          disabled={!hasSteps}
          title={timeline.playing ? 'Pause' : 'Play through steps'}
          onClick={() => setPlaying(!timeline.playing)}
        >
          {timeline.playing ? <Pause size={15} /> : <Play size={15} />}
        </Button>
        <Button variant="ghost" className="h-7 w-7 p-0" disabled={!hasSteps} title="Next step" onClick={() => stepBy(1)}>
          <ChevronRight size={15} />
        </Button>
      </div>

      <div className="flex items-center gap-1" title="Playback rate (steps per second)">
        <Input
          type="number"
          className="h-7 w-14"
          value={timeline.fps}
          min={1}
          max={240}
          disabled={!hasSteps}
          onChange={e => setFps(Number(e.target.value))}
        />
        <span className="text-[10px] text-muted-foreground">fps</span>
      </div>

      {isStep && (
        <Input
          type="number"
          className="h-7 w-20"
          value={timeline.step ?? ''}
          min={minStep}
          max={maxStep}
          placeholder="step"
          onChange={(e) => {
            const v = e.target.value
            setStep(v === '' ? null : Math.max(minStep, Math.min(maxStep, Number(v))))
          }}
        />
      )}

      {isDesktop && (
        <>
          <Button variant="ghost" className="h-7 w-7 p-0" title="Reset zoom" onClick={onResetZoom}>
            <Maximize size={14} />
          </Button>
          <Button variant="ghost" className="h-7 px-2 text-xs text-muted-foreground" title="Clear all filters" onClick={onClearFilters}>
            Clear all filters
          </Button>
        </>
      )}

      <Button variant="ghost" className="ml-auto h-7 w-7 p-0" onClick={onToggleCollapse} title={collapsed ? 'Expand tracker' : 'Collapse tracker'}>
        {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </Button>
    </div>
  )
}

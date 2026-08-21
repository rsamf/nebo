import { useMemo } from 'react'
import { ImageIcon, LineChart as LineIcon, BarChart3, ScatterChart, Boxes } from 'lucide-react'
import { Switch } from '@/components/ui/switch'
import { useStore, type Settings as SettingsType } from '@/store'
import { useComparisonContext } from '@/hooks/useComparisonContext'

function ChartSlider({
  label,
  value,
  min,
  max,
  step,
  format,
  onChange,
}: {
  label: string
  value: number
  min: number
  max: number
  step: number
  format?: (v: number) => string
  onChange: (next: number) => void
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span>{label}</span>
        <span className="text-muted-foreground tabular-nums">
          {format ? format(value) : value}
        </span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-full h-1 accent-primary cursor-pointer"
      />
    </div>
  )
}

// Per-run settings surface that lives in the right panel. Hosts the
// global chart knobs (line/histogram smoothing and bin count) and any
// image-label controls registered by the currently-viewed run.
export function RightPanelSettings() {
  const labelKeySettings = useStore(s => s.labelKeySettings)
  const setLabelKeyVisible = useStore(s => s.setLabelKeyVisible)
  const setLabelKeyOpacity = useStore(s => s.setLabelKeyOpacity)
  const settings = useStore(s => s.settings)
  const updateSetting = useStore(s => s.updateSetting)
  const runs = useStore(s => s.runs)
  const { runIds } = useComparisonContext()

  // labelKeySettings is global (keyed by loggable|image|key), but each
  // controls section should only surface entries that match images
  // present in the currently-viewed run(s). Otherwise switching runs
  // leaves stale toggles in place for label keys that don't exist here.
  const visibleEntries = useMemo(() => {
    if (runIds.length === 0) return [] as [string, typeof labelKeySettings[string]][]
    const activePairs = new Set<string>()
    for (const rid of runIds) {
      const run = runs.get(rid)
      if (!run) continue
      for (const [loggableId, images] of Object.entries(run.loggableImages)) {
        for (const img of images) {
          activePairs.add(`${loggableId}|${img.name}`)
        }
      }
    }
    return Object.entries(labelKeySettings).filter(([triple]) => {
      const [loggable, image] = triple.split('|')
      return activePairs.has(`${loggable}|${image}`)
    })
  }, [labelKeySettings, runs, runIds])

  return (
    <div className="h-full overflow-auto p-4 space-y-6">
      <section>
        <div className="flex items-center gap-2 mb-3">
          <LineIcon className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Line charts</h3>
        </div>
        <ChartSlider
          label="Smoothing"
          value={settings.lineSmoothing}
          min={0}
          max={0.99}
          step={0.01}
          format={(v) => v.toFixed(2)}
          onChange={(v) => updateSetting<keyof SettingsType>('lineSmoothing', v)}
        />
      </section>

      <section>
        <div className="flex items-center gap-2 mb-3">
          <ScatterChart className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Scatter charts</h3>
        </div>
        <div className="space-y-3">
          <ChartSlider
            label="Point opacity"
            value={settings.scatterPointOpacity}
            min={0.1}
            max={1}
            step={0.05}
            format={(v) => `${Math.round(v * 100)}%`}
            onChange={(v) => updateSetting<keyof SettingsType>('scatterPointOpacity', v)}
          />
          <ChartSlider
            label="Point size"
            value={settings.scatterPointSize}
            min={0.25}
            max={1}
            step={0.05}
            format={(v) => `${Math.round(v * 100)}%`}
            onChange={(v) => updateSetting<keyof SettingsType>('scatterPointSize', v)}
          />
        </div>
      </section>

      <section>
        <div className="flex items-center gap-2 mb-3">
          <BarChart3 className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Histogram charts</h3>
        </div>
        <div className="space-y-3">
          <ChartSlider
            label="Smoothing"
            value={settings.histogramSmoothing}
            min={0}
            max={0.99}
            step={0.01}
            format={(v) => v.toFixed(2)}
            onChange={(v) => updateSetting<keyof SettingsType>('histogramSmoothing', v)}
          />
          <ChartSlider
            label="Bins"
            value={settings.histogramBinCount}
            min={5}
            max={100}
            step={1}
            onChange={(v) => updateSetting<keyof SettingsType>('histogramBinCount', v)}
          />
        </div>
      </section>

      <section>
        <div className="flex items-center gap-2 mb-3">
          <Boxes className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">3D scenes</h3>
        </div>
        <div className="space-y-3">
          <ChartSlider
            label="Model opacity"
            value={settings.bodyOpacity}
            min={0}
            max={1}
            step={0.05}
            format={(v) => `${Math.round(v * 100)}%`}
            onChange={(v) => updateSetting<keyof SettingsType>('bodyOpacity', v)}
          />
          <label className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Show collision geometry</span>
            <input
              type="checkbox"
              className="accent-primary"
              checked={settings.showCollision}
              onChange={(e) => updateSetting<keyof SettingsType>('showCollision', e.target.checked)}
            />
          </label>
          <ChartSlider
            label="Collision opacity"
            value={settings.collisionOpacity}
            min={0}
            max={1}
            step={0.05}
            format={(v) => `${Math.round(v * 100)}%`}
            onChange={(v) => updateSetting<keyof SettingsType>('collisionOpacity', v)}
          />
          <div className="flex items-center gap-2">
            <span className="flex-1 text-xs text-muted-foreground">
              Model offset
            </span>
            {(['modelOffsetX', 'modelOffsetY'] as const).map((key, i) => (
              <label key={key} className="flex items-center gap-1">
                <span className="text-[10px] text-muted-foreground">
                  {i === 0 ? 'x' : 'y'}
                </span>
                <input
                  type="number"
                  min={0}
                  step={0.5}
                  value={settings[key]}
                  onChange={(e) => updateSetting<keyof SettingsType>(
                    key, Math.max(0, Number(e.target.value)),
                  )}
                  className="h-7 w-16 rounded-md border border-border bg-transparent px-1.5 text-xs"
                />
              </label>
            ))}
          </div>
          <p className="text-[10px] leading-snug text-muted-foreground">
            Spacing between instances, laid out on a 2-column grid. 0 keeps
            each instance at its logged position.
          </p>
          <label className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Tint instances</span>
            <input
              type="checkbox"
              className="accent-primary"
              checked={settings.tintInstances}
              onChange={(e) => updateSetting<keyof SettingsType>('tintInstances', e.target.checked)}
            />
          </label>
        </div>
      </section>

      {visibleEntries.length > 0 && (
      <section>
        <div className="flex items-center gap-2 mb-3">
          <ImageIcon className="h-4 w-4 text-muted-foreground" />
          <h3 className="text-sm font-medium">Image labels</h3>
          <span className="text-xs text-muted-foreground">
            ({visibleEntries.length})
          </span>
        </div>

        <div className="space-y-4">
          {visibleEntries.map(([triple, s]) => {
              const [loggable, image, key] = triple.split('|')
              return (
                <div key={triple} className="space-y-1">
                  <div className="flex items-center justify-between gap-2">
                    <span
                      className="text-xs text-muted-foreground truncate"
                      title={`${loggable} > ${image} > ${key}`}
                    >
                      {loggable} › {image} ›{' '}
                      <span className="font-medium text-foreground">{key}</span>
                    </span>
                    <Switch
                      checked={s.visible}
                      onCheckedChange={(v) => setLabelKeyVisible(loggable, image, key, v)}
                    />
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={s.opacity}
                    onChange={(e) =>
                      setLabelKeyOpacity(loggable, image, key, Number(e.target.value))
                    }
                    disabled={!s.visible}
                    className="w-full h-1 accent-primary cursor-pointer disabled:opacity-40"
                    aria-label={`${key} opacity`}
                  />
              </div>
            )
          })}
        </div>
      </section>
      )}
    </div>
  )
}

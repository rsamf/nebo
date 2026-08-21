import { useMemo } from 'react'
import { useStore } from '@/store'
import { MobileSheet } from './MobileSheet'

// View-settings sheet: the desktop right-panel chart knobs as
// touch-sized sliders. Label opacity applies to every label key
// registered on images in this run (the desktop panel exposes them
// individually; one slider covers the mobile case).
export function MobileSettingsSheet({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const settings = useStore(s => s.settings)
  const updateSetting = useStore(s => s.updateSetting)
  const labelKeySettings = useStore(s => s.labelKeySettings)
  const setLabelKeyOpacity = useStore(s => s.setLabelKeyOpacity)
  // Keyed on the images slice, not the run — the run clones on every
  // event, the images record only when an image lands.
  const loggableImages = useStore(s => s.runs.get(runId)?.loggableImages)

  // Label keys present on this run's images (same scoping rule as
  // RightPanelSettings.visibleEntries).
  const visibleLabelTriples = useMemo(() => {
    if (!loggableImages) return [] as string[]
    const activePairs = new Set<string>()
    for (const [loggableId, images] of Object.entries(loggableImages)) {
      for (const img of images) activePairs.add(`${loggableId}|${img.name}`)
    }
    return Object.keys(labelKeySettings).filter(triple => {
      const [loggable, image] = triple.split('|')
      return activePairs.has(`${loggable}|${image}`)
    })
  }, [loggableImages, labelKeySettings])

  const labelOpacity = useMemo(() => {
    if (visibleLabelTriples.length === 0) return 70
    const sum = visibleLabelTriples.reduce(
      (acc, t) => acc + (labelKeySettings[t]?.opacity ?? 70),
      0,
    )
    return Math.round(sum / visibleLabelTriples.length)
  }, [visibleLabelTriples, labelKeySettings])

  const setAllLabelOpacities = (opacity: number) => {
    for (const triple of visibleLabelTriples) {
      const [loggable, image, key] = triple.split('|')
      setLabelKeyOpacity(loggable, image, key, opacity)
    }
  }

  const reset = () => {
    updateSetting('lineSmoothing', 0)
    updateSetting('scatterPointOpacity', 0.8)
    updateSetting('scatterPointSize', 0.5)
    updateSetting('bodyOpacity', 1)
    updateSetting('showCollision', false)
    updateSetting('collisionOpacity', 0.35)
    updateSetting('modelOffsetX', 2)
    updateSetting('modelOffsetY', 2)
    setAllLabelOpacities(70)
  }

  return (
    <MobileSheet onClose={onClose}>
      <div className="px-4 pb-8">
        <div className="mb-1 text-base font-semibold">View settings</div>
        <div className="mb-4 text-[11px] text-muted-foreground">
          Applies to charts and image cards in this run
        </div>
        <div className="flex flex-col gap-4">
          <SettingSlider
            label="Smoothing"
            value={settings.lineSmoothing}
            min={0}
            max={0.99}
            step={0.01}
            format={v => v.toFixed(2)}
            onChange={v => updateSetting('lineSmoothing', v)}
          />
          <SettingSlider
            label="Point opacity"
            value={settings.scatterPointOpacity}
            min={0.1}
            max={1}
            step={0.05}
            format={v => `${Math.round(v * 100)}%`}
            onChange={v => updateSetting('scatterPointOpacity', v)}
          />
          <SettingSlider
            label="Point size"
            value={settings.scatterPointSize}
            min={0.25}
            max={1}
            step={0.05}
            format={v => `${Math.round(v * 100)}%`}
            onChange={v => updateSetting('scatterPointSize', v)}
          />
          <SettingSlider
            label="Model opacity"
            value={settings.bodyOpacity}
            min={0}
            max={1}
            step={0.05}
            format={v => `${Math.round(v * 100)}%`}
            onChange={v => updateSetting('bodyOpacity', v)}
          />
          <label className="flex items-center justify-between text-xs">
            <span className="text-[13px] font-medium">Show collision geometry</span>
            <input
              type="checkbox"
              className="accent-primary"
              checked={settings.showCollision}
              onChange={e => updateSetting('showCollision', e.target.checked)}
            />
          </label>
          <SettingSlider
            label="Collision opacity"
            value={settings.collisionOpacity}
            min={0}
            max={1}
            step={0.05}
            format={v => `${Math.round(v * 100)}%`}
            onChange={v => updateSetting('collisionOpacity', v)}
          />
          <div className="flex items-center gap-2">
            <span className="flex-1 text-[13px] font-medium">Model offset</span>
            {(['modelOffsetX', 'modelOffsetY'] as const).map((key, i) => (
              <label key={key} className="flex items-center gap-1">
                <span className="text-[10px] text-muted-foreground">
                  {i === 0 ? 'x' : 'y'}
                </span>
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step={0.5}
                  value={settings[key]}
                  onChange={e => updateSetting(key, Math.max(0, Number(e.target.value)))}
                  className="h-8 w-16 rounded-md border border-border bg-transparent px-2 text-xs"
                />
              </label>
            ))}
          </div>
          {visibleLabelTriples.length > 0 && (
            <SettingSlider
              label="Image label opacity"
              value={labelOpacity}
              min={0}
              max={100}
              step={5}
              format={v => `${Math.round(v)}%`}
              onChange={setAllLabelOpacities}
            />
          )}
        </div>
        <button
          onClick={reset}
          className="mt-5 w-full rounded-[10px] border border-border py-2.5 text-xs font-medium text-muted-foreground"
        >
          Reset to defaults
        </button>
      </div>
    </MobileSheet>
  )
}

function SettingSlider({
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
  format: (v: number) => string
  onChange: (v: number) => void
}) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-[13px]">
        <span className="font-medium">{label}</span>
        <span className="tabular-nums text-muted-foreground">{format(value)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={e => onChange(Number(e.target.value))}
        className="h-8 w-full accent-primary"
        aria-label={label}
      />
    </div>
  )
}

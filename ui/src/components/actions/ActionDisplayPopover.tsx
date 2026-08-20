import { Boxes } from 'lucide-react'
import { useStore } from '@/store'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

/**
 * Scene display controls: model opacity, collision visibility + opacity,
 * instance tinting.
 *
 * These write the *global* `settings` keys the Settings panel also edits —
 * one source of truth, surfaced where the user is looking. A per-card
 * override layer would leave two places disagreeing about what "collision
 * opacity" means.
 */
export function ActionDisplayPopover({ showTint }: { showTint: boolean }) {
  const settings = useStore(s => s.settings)
  const updateSetting = useStore(s => s.updateSetting)

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" className="h-6 w-6" title="Scene display">
          <Boxes className="h-3.5 w-3.5" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-60 space-y-3" align="end">
        <Slider
          label="Model opacity"
          value={settings.bodyOpacity}
          onChange={v => updateSetting('bodyOpacity', v)}
        />
        <label className="flex items-center justify-between text-xs">
          <span className="text-muted-foreground">Show collision</span>
          <input
            type="checkbox"
            className="accent-primary"
            checked={settings.showCollision}
            onChange={e => updateSetting('showCollision', e.target.checked)}
          />
        </label>
        <Slider
          label="Collision opacity"
          value={settings.collisionOpacity}
          disabled={!settings.showCollision}
          onChange={v => updateSetting('collisionOpacity', v)}
        />
        {showTint && (
          <label className="flex items-center justify-between text-xs">
            <span className="text-muted-foreground">Tint instances</span>
            <input
              type="checkbox"
              className="accent-primary"
              checked={settings.tintInstances}
              onChange={e => updateSetting('tintInstances', e.target.checked)}
            />
          </label>
        )}
      </PopoverContent>
    </Popover>
  )
}

function Slider({ label, value, onChange, disabled }: {
  label: string
  value: number
  onChange: (v: number) => void
  disabled?: boolean
}) {
  return (
    <label className={`block space-y-1 ${disabled ? 'opacity-40' : ''}`}>
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="tabular-nums">{Math.round(value * 100)}%</span>
      </div>
      <input
        type="range"
        min={0}
        max={1}
        step={0.05}
        value={value}
        disabled={disabled}
        onChange={e => onChange(Number(e.target.value))}
        className="w-full accent-primary"
      />
    </label>
  )
}

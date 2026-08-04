import { useStore } from '@/store'
import { RunList } from '@/components/runs/RunList'
import { SettingsPanel } from './SettingsPanel'
import { Link2, Link2Off } from 'lucide-react'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

export function Sidebar() {
  const connected = useStore(s => s.connected)

  return (
    <div className="flex flex-col h-full bg-sidebar border-r border-sidebar-border">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-sidebar-border">
        <div className="flex items-center gap-2">
          <img src="/favicon.png" alt="" className="h-5 w-5" />
          <h1 className="text-lg font-semibold text-sidebar-foreground">Nebo</h1>
        </div>
        <div className="flex items-center gap-1">
          {connected ? (
            <Tooltip>
                <TooltipTrigger asChild>
                    <Link2 className="h-4 w-4 text-green-500" />
                </TooltipTrigger>
                <TooltipContent align="start">
                    Connected
                </TooltipContent>
            </Tooltip>
          ) : (
            <Tooltip>
                <TooltipTrigger asChild>
                    <Link2Off className="h-4 w-4 text-muted-foreground" />
                </TooltipTrigger>
                <TooltipContent align="start">
                    Disconnected
                </TooltipContent>
            </Tooltip>
          )}
          <SettingsPanel />
        </div>
      </div>

      {/* Run list */}
      <div className="flex-1 overflow-hidden">
        <RunList />
      </div>
    </div>
  )
}

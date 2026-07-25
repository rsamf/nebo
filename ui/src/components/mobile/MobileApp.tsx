import { useStore } from '@/store'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { MobileRunList } from './MobileRunList'
import { MobileGroupPage } from './MobileGroupPage'
import { MobileRunView } from './MobileRunView'

// Mobile shell: full-screen switching between the run list, a group
// page and the run view. Comparison groups are a desktop feature — a
// selected `cmp:` id falls back to the list.
export function MobileApp() {
  const selectedRunId = useStore(s => s.selectedRunId)
  const selectedGroup = useStore(s => s.selectedGroup)

  if (selectedGroup) {
    return (
      <ErrorBoundary label="MobileGroupPage">
        <MobileGroupPage path={selectedGroup} />
      </ErrorBoundary>
    )
  }
  if (selectedRunId && !selectedRunId.startsWith('cmp:')) {
    return (
      <ErrorBoundary label="MobileRunView">
        <MobileRunView runId={selectedRunId} />
      </ErrorBoundary>
    )
  }
  return (
    <ErrorBoundary label="MobileRunList">
      <MobileRunList />
    </ErrorBoundary>
  )
}

import type { ClaimEvent } from '../api'
import { formatTime } from '../api'

const ACTOR: Record<ClaimEvent['actor'], string> = {
  customer: 'Customer',
  user: 'Adjuster',
  system: 'System',
}

const DOT: Record<ClaimEvent['actor'], string> = {
  customer: 'bg-sky-500',
  user: 'bg-indigo-600',
  system: 'bg-slate-400',
}

function describe(e: ClaimEvent) {
  if (e.type === 'claim.opened') {
    return `Claim opened for policy ${e.data.policy_no}${e.data.policy_verified ? '' : ' (not found in policy system)'}`
  }
  if (e.from_status && e.to_status) return `${e.from_status.replace(/_/g, ' ')} → ${e.to_status.replace(/_/g, ' ')}`
  return e.type.replace(/[._]/g, ' ')
}

export default function Timeline({ events }: { events: ClaimEvent[] }) {
  return (
    <ol className="relative space-y-4 border-l border-slate-200 pl-5">
      {events.map((e) => (
        <li key={e.id} className="relative">
          <span className={`absolute -left-[26px] top-1.5 h-2.5 w-2.5 rounded-full ring-4 ring-slate-50 ${DOT[e.actor]}`} />
          <p className="text-sm font-medium">{describe(e)}</p>
          <p className="text-xs text-slate-500">
            {ACTOR[e.actor]} · {formatTime(e.at)} ·{' '}
            <span className="font-mono" title={`Event hash ${e.hash}`}>#{e.seq} {e.hash.slice(0, 10)}</span>
          </p>
        </li>
      ))}
    </ol>
  )
}

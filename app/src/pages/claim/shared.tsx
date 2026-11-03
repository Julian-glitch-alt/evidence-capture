import { api, formatDueDate, todayIso, type Answers, type Check, type ClaimDetail, type Damage, type Item, type ProposalFlag, type QueueClaim } from '../../api'

export { formatDueDate, todayIso }

export const REVIEW_STEPS = ['coverage', 'liability', 'damage', 'estimate'] as const
export type ReviewStep = (typeof REVIEW_STEPS)[number]

export const REVIEW_STEP_TITLES: Record<ReviewStep, string> = {
  coverage: 'Coverage and vehicle',
  liability: 'What happened and liability',
  damage: 'Damage',
  estimate: 'Estimate and reserve',
}

export function stepOfFlag(step: string | null | undefined): ReviewStep {
  if (step === 'coverage' || step === 'liability' || step === 'damage' || step === 'estimate') return step
  return 'liability'
}

/** Queue lanes that still need a decision. */
export const NEEDS_LANES = ['siu_referral', 'adjuster_review', 'fast_track', 'standard']

export function isNeedsLane(lane: string | null | undefined) {
  return lane != null && NEEDS_LANES.includes(lane)
}

export function lanePillClass(lane: string | null | undefined): 'siu' | 'review' | 'fast' | 'standard' | 'waiting' | 'decided' {
  if (lane === 'siu_referral') return 'siu'
  if (lane === 'adjuster_review') return 'review'
  if (lane === 'fast_track') return 'fast'
  if (lane === 'standard') return 'standard'
  if (lane === 'decided') return 'decided'
  return 'waiting'
}

export function lanePillLabel(lane: string | null | undefined, decidedLabel?: string | null) {
  if (lane === 'siu_referral') return 'SIU'
  if (lane === 'adjuster_review') return 'Adjuster review'
  if (lane === 'fast_track') return 'Fast track'
  if (lane === 'standard') return 'Standard'
  if (lane == null) {
    if (decidedLabel) return decidedLabel
    return 'Waiting for claimant'
  }
  return laneName(lane)
}

/** "2026-10-27" -> "Oct 31, 2026"; anything else passes through. */
export function formatDateLong(value: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim())
  if (!m) return value
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const month = Number(m[2])
  if (month < 1 || month > 12) return value
  return `${months[month - 1]} ${Number(m[3])}, ${m[1]}`
}

export function formatAnswer(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (Array.isArray(value)) {
    const parts = value.filter((v): v is string => typeof v === 'string' && v !== '')
    if (parts.length === 0) return '—'
    return parts.join(', ')
  }
  if (typeof value === 'string') {
    const t = value.trim()
    if (!t) return '—'
    if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return formatDateLong(t)
    return t
  }
  return '—'
}

export function queueClaimLane(c: QueueClaim): string | null {
  if (c.lane && isNeedsLane(c.lane)) return c.lane
  if (c.lane) return c.lane
  return null
}

/** One numbered finding for the review and the loss notice pins. */
export interface OrderedFinding {
  entry: ProposalFlag
  /** Display number, 1-based in step order then the proposal's order. */
  n: number
}

/** Flags then notes, numbered in step order (coverage, liability, damage, estimate), then the proposal's order. */
export function orderedFindings(claim: ClaimDetail): OrderedFinding[] {
  const proposal = claim.proposal
  if (!proposal || proposal.status !== 'ready') return []
  const all = [...proposal.flags, ...proposal.notes]
  const rank = (s: string | null | undefined) => {
    const i = REVIEW_STEPS.indexOf(stepOfFlag(s))
    return i < 0 ? REVIEW_STEPS.length : i
  }
  return all
    .map((entry, i) => ({ entry, i }))
    .sort((a, b) => rank(a.entry.step) - rank(b.entry.step) || a.i - b.i)
    .map(({ entry }, k) => ({ entry, n: k + 1 }))
}

/** Findings that need a Confirm / False alarm (notes are information only). */
export function reviewableFindings(claim: ClaimDetail): OrderedFinding[] {
  return orderedFindings(claim).filter((f) => f.entry.level !== 'note')
}

export function reviewedCount(claim: ClaimDetail): number {
  const reviewed = claim.review?.flags ?? {}
  return reviewableFindings(claim).filter((f) => reviewed[f.entry.key]?.verdict === 'confirmed' || reviewed[f.entry.key]?.verdict === 'false_alarm').length
}

/** Scroll an answer row or figure into view and flash it once. */
export function scrollToAndFlash(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  el.scrollIntoView({ block: 'center' })
  el.classList.remove('ev-flash')
  void el.offsetWidth
  el.classList.add('ev-flash')
}

/** "photo.vin_photo" -> a readable source line for the evidence. */
export function readableSource(source: string, titles: Record<string, string>): string {
  if (source.startsWith('consistency.')) return 'Statement vs photos'
  if (source.startsWith('photo.')) {
    const slot = source.slice('photo.'.length)
    const title = titles[slot] ?? slot
    return `${title} photo check`
  }
  if (source.startsWith('coverage.')) return 'Coverage check'
  if (source.startsWith('claimtype.')) return 'Claim type check'
  if (source === 'submit') return 'Claim details'
  if (source === 'estimate') return 'Damage reading'
  if (source === 'capture.screen') return 'Screen or print check'
  if (source.startsWith('capture.')) return 'Capture trail'
  if (source.startsWith('fnol.')) return 'First notice vs statement'
  return source
}

export function usd(n: number) {
  return `$${n.toLocaleString('en-US')}`
}

export const LANE_LABEL: Record<string, string> = {
  siu_referral: 'SIU',
  adjuster_review: 'Adjuster review',
  fast_track: 'Fast track',
  standard: 'Standard',
}

export function laneClass(lane: string | null): 'siu' | 'review' | 'fast' | 'standard' {
  if (lane === 'siu_referral') return 'siu'
  if (lane === 'adjuster_review') return 'review'
  if (lane === 'fast_track') return 'fast'
  return 'standard'
}

export function laneName(lane: string | null) {
  if (!lane) return '—'
  return LANE_LABEL[lane] ?? lane
}

export function holderOf(claim: ClaimDetail): string {
  const p = claim.policy.data
  if (p) return p.holder.name
  const v = claim.answers['holder_name'] ?? claim.answers['holder']
  if (typeof v === 'string' && v) return v
  return '—'
}

export function vehicleOf(claim: ClaimDetail): string {
  const p = claim.policy.data
  if (p && p.vehicles[0]) {
    const v = p.vehicles[0]
    return `${v.year} ${v.make} ${v.model}`
  }
  const v = claim.answers['vehicle'] ?? claim.answers['vehicle_description']
  if (typeof v === 'string' && v) return v
  return '—'
}

function firstString(answers: Answers, keys: string[]): string | null {
  for (const k of keys) {
    const v = answers[k]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return null
}

export function lossDateOf(claim: ClaimDetail): string {
  return (
    firstString(claim.answers, ['loss_date', 'date_of_loss', 'lossDate', 'incident_date']) ?? '—'
  )
}

export function lossCauseOf(claim: ClaimDetail, fallback: string | null): string {
  const direct = firstString(claim.answers, ['cause', 'loss_cause', 'lossCause', 'description', 'loss_description'])
  if (direct) return direct
  if (fallback) return fallback
  return '—'
}

export function activeFlags(claim: ClaimDetail) {
  const proposal = claim.proposal
  if (!proposal || proposal.status !== 'ready') return []
  const reviewed = claim.review?.flags ?? {}
  return proposal.flags.filter((f) => reviewed[f.key]?.verdict !== 'false_alarm')
}

export function likelyRange(claim: ClaimDetail): string {
  const proposal = claim.proposal
  if (!proposal || proposal.status !== 'ready') return '—'
  const likely = proposal.estimate.likely
  if (!likely) return 'no priced range'
  return `${usd(likely[0])}–${usd(likely[1])}`
}

// Only a verified policy and a pass on both photo checks may say the vehicle matches (server: report.vehicle_matches_policy).
export function vinPlateLine(claim: ClaimDetail): boolean {
  const vin = claim.checks.find((c) => c.check_id === 'photo.vin_photo')
  const plate = claim.checks.find((c) => c.check_id === 'photo.plate')
  return claim.policy?.verified === true && vin?.result === 'pass' && plate?.result === 'pass'
}

export function shortHash(hash: string) {
  if (hash.length <= 16) return hash
  return `${hash.slice(0, 8)}…${hash.slice(-4)}`
}

export function Pill({ kind, icon, word }: { kind: 'review' | 'conflict' | 'pass' | 'note'; icon: string; word: string }) {
  return (
    <span className={`desk-pill ${kind}`}>
      <span aria-hidden="true">{icon}</span>
      {word}
    </span>
  )
}

export function flagPill(level: 'conflict' | 'review' | 'note', n: number) {
  if (level === 'conflict') return <Pill kind="conflict" icon="!" word={`F${n} Conflict`} />
  if (level === 'review') return <Pill kind="review" icon="?" word={`F${n} Review`} />
  return <Pill kind="note" icon="•" word={`F${n} Note`} />
}

export function checkPill(result: Check['result']) {
  if (result === 'pass' || result === 'value') return <Pill kind="pass" icon="✓" word={result === 'value' ? 'Info' : 'Pass'} />
  if (result === 'fail') return <Pill kind="conflict" icon="!" word="Fail" />
  return <Pill kind="review" icon="?" word="Review" />
}

export function damageTypeWords(raw: string | null | undefined): string {
  const parts = String(raw ?? '').toLowerCase().split('_').filter(Boolean)
  if (parts.length === 0) return '—'
  if (parts.length >= 2 && parts[parts.length - 1] === 'scuff') {
    const head = parts.slice(0, -1).join(' ')
    const Head = head.charAt(0).toUpperCase() + head.slice(1)
    return `${Head} or scuff`
  }
  const [first, ...rest] = parts
  return [first.charAt(0).toUpperCase() + first.slice(1), ...rest].join(' ')
}

export function checkGroupName(checkId: string): 'Coverage' | 'Photos' | 'Statement vs photos' {
  if (checkId.startsWith('consistency.')) return 'Statement vs photos'
  if (
    checkId.startsWith('coverage') ||
    checkId.startsWith('policy') ||
    checkId.startsWith('eligibility') ||
    checkId.includes('deductible')
  )
    return 'Coverage'
  return 'Photos'
}

export function slotTitles(claim: ClaimDetail): Record<string, string> {
  const titles: Record<string, string> = {}
  for (const section of claim.layout.sections) {
    for (const slot of section.slots) titles[slot.id] = slot.title
  }
  return titles
}

export function photoLabel(claim: ClaimDetail, item: Item): string {
  const titles = slotTitles(claim)
  const base = titles[item.step_id] ?? item.step_id
  const sameSlot = claim.items
    .filter((i) => i.step_id === item.step_id && !i.superseded)
    .sort((a, b) => a.received_at.localeCompare(b.received_at))
  const idx = sameSlot.findIndex((i) => i.id === item.id)
  return idx >= 0 ? `${base} #${idx + 1}` : base
}

export function damagesForItem(claim: ClaimDetail, itemId: string): Damage[] {
  return (claim.damage?.damages ?? []).filter((d) => d.item_id === itemId)
}

export function itemById(claim: ClaimDetail, itemId: string): Item | undefined {
  return claim.items.find((i) => i.id === itemId)
}

export function EvidenceThumb({
  claim,
  itemId,
  number,
  onOpen,
}: {
  claim: ClaimDetail
  itemId: string
  number?: number
  onOpen: (itemId: string) => void
}) {
  const item = itemById(claim, itemId)
  if (!item) return null
  const damages = damagesForItem(claim, itemId)
  const label = photoLabel(claim, item)
  if (item.removed_at) {
    return (
      <span className="desk-thumb grid place-items-center bg-[var(--cell)] text-[12px] text-[var(--ink-3)]">
        Removed
      </span>
    )
  }
  return (
    <button type="button" className="desk-thumb" onClick={() => onOpen(itemId)} aria-label={`Open photo ${label}`}>
      <img src={api.fileUrl(item.id)} alt={label} loading="lazy" />
      {damages.map((d, i) =>
        d.box ? (
          <span
            key={d.key}
            className="desk-box"
            style={{
              left: `${d.box[0] / 10}%`,
              top: `${d.box[1] / 10}%`,
              width: `${Math.max(0, d.box[2] - d.box[0]) / 10}%`,
              height: `${Math.max(0, d.box[3] - d.box[1]) / 10}%`,
            }}
          >
            <i>{typeof number === 'number' ? number : (d.number > 0 ? d.number : i + 1)}</i>
          </span>
        ) : null,
      )}
      <span className="tag">{label}</span>
    </button>
  )
}

import { useState } from 'react'
import { api, formatTime, type ClaimDetail, type DecisionInput } from '../../api'
import { laneName } from './shared'

const ACTIONS = [
  { key: 'approve', label: 'Approve', hint: 'Approves the proposal (lane, next step, suggested reserve). Not a payment.' },
  { key: 'edit_approve', label: 'Edit & approve', hint: 'Change the lane or the suggested reserve; the change and your reason go to the claim file.' },
  { key: 'send_back', label: 'Send back', hint: 'Records which photos the claimant should retake.' },
  { key: 'refer_siu', label: 'Refer to SIU', hint: 'Creates an "SIU referral review" activity in the claims system.' },
  { key: 'field_inspection', label: 'Field inspection', hint: 'Creates a "Schedule field inspection" activity.' },
]

const LANE_OPTIONS = [
  { key: 'siu_referral', label: 'Refer to SIU' },
  { key: 'adjuster_review', label: 'Adjuster review' },
  { key: 'fast_track', label: 'Fast track' },
  { key: 'standard', label: 'Standard' },
]

function usd(n: number) {
  return `$${n.toLocaleString('en-US')}`
}

export default function DecisionBar({
  claim,
  onDecided,
}: {
  claim: ClaimDetail
  onDecided: (c: ClaimDetail) => void
}) {
  const proposal = claim.proposal
  const [action, setAction] = useState<string | null>(null)
  const [reason, setReason] = useState('')
  const [lane, setLane] = useState('')
  const [reserve, setReserve] = useState('')
  const [slots, setSlots] = useState<string[]>([])
  const [decideError, setDecideError] = useState<string>()
  const [sending, setSending] = useState(false)

  if (!proposal || proposal.status !== 'ready') return null

  if (claim.decision) {
    const d = claim.decision
    // The latest message that carries the decision: decision.recorded (on_ready) or the
    // reviewed evidence.ready (on_decision); never the earlier pending_review or round.
    const message = [...(claim.report?.outbox ?? [])]
      .reverse()
      .find((m) => m.event === 'decision.recorded' || m.event === 'evidence.ready')
    const titles: Record<string, string> = {}
    for (const section of claim.layout.sections) {
      for (const slot of section.slots) titles[slot.id] = slot.title
    }
    return (
      <section aria-label="Decision" className="border border-[var(--ink)]">
        <div className="border-b border-[var(--rule)] bg-[var(--cell)] px-3 py-2">
          <p className="m-0 text-[13px]">
            <strong>Decided: {d.label}</strong> by {d.decided_by}, {formatTime(d.decided_at)}. Locked.
          </p>
          <p className="m-0 mt-1 text-[12px] text-[var(--ink-2)]">
            Lane: {laneName(d.lane_before)} → {laneName(d.lane_after)}
            {' · '}Suggested initial reserve: {d.reserve_before !== null ? usd(d.reserve_before) : '—'}
            {' → '}
            {d.reserve_after !== null ? usd(d.reserve_after) : '—'}
          </p>
          {d.reason && <p className="m-0 mt-1 text-[12px] text-[var(--ink-2)]">Reason: {d.reason}</p>}
          {d.slots.length > 0 && (
            <p className="m-0 mt-1 text-[12px] text-[var(--ink-2)]">
              Photos requested: {d.slots.map((s) => titles[s] ?? s).join(', ')}
            </p>
          )}
          <p className="m-0 mt-1 text-[12px] text-[var(--ink-2)]">
            {message
              ? message.delivered_at
                ? `Delivered ${formatTime(message.delivered_at)}`
                : `Not delivered: ${message.last_error ?? 'pending'} (${message.attempts} attempts)`
              : 'Not delivered yet'}
          </p>
        </div>
      </section>
    )
  }

  const titles: Record<string, string> = {}
  for (const section of claim.layout.sections) {
    for (const slot of section.slots) titles[slot.id] = slot.title
  }
  const photoSlots = claim.layout.sections.flatMap((s) => s.slots).filter((s) => s.kind === 'photo')
  const current = ACTIONS.find((a) => a.key === action)
  const reasonRequired = action !== null && (action !== 'approve' || proposal.flags.length > 0)

  const open = (key: string) => {
    setAction(key)
    setDecideError(undefined)
    setReason('')
    setLane(proposal.lane)
    setReserve(proposal.suggested_reserve !== null ? String(proposal.suggested_reserve) : '')
    setSlots([])
  }

  const confirm = async () => {
    if (!action) return
    const trimmed = reason.trim()
    if (reasonRequired && trimmed.length < 5) {
      setDecideError('A reason of at least 5 characters is required.')
      return
    }
    if (trimmed.length > 500) {
      setDecideError('The reason is too long.')
      return
    }
    setSending(true)
    setDecideError(undefined)
    try {
      const body: DecisionInput = { action }
      if (trimmed) body.reason = trimmed
      if (action === 'edit_approve') {
        body.lane = lane
        if (reserve.trim() !== '') body.suggested_reserve = Number(reserve.trim())
      }
      if (action === 'send_back') body.slots = slots
      onDecided(await api.decide(claim.id, body))
      setAction(null)
    } catch (e) {
      setDecideError(e instanceof Error ? e.message : 'Could not record the decision')
    } finally {
      setSending(false)
    }
  }

  return (
    <section aria-label="Decision" className="border border-[var(--ink)]">
      <p className="m-0 border-b border-[var(--rule)] px-3 py-2 text-[12px] text-[var(--ink-2)]">
        {claim.delivery_mode === 'on_decision'
          ? 'Nothing has been sent to the insurer yet. Your decision sends the reviewed report.'
          : 'The report was sent when the analysis finished; your decision is sent as an update.'}
      </p>
      <div className="flex flex-wrap items-center gap-2 px-3 py-[10px]" role="toolbar" aria-label="Decide">
        <span className="desk-label mr-[6px]">Your decision</span>
        {ACTIONS.map((a) => (
          <button
            key={a.key}
            type="button"
            data-act={a.key}
            onClick={() => open(a.key)}
            className={`desk-act${action === a.key ? ' on' : ''}${a.key === 'approve' ? ' primary' : ''}`}
          >
            {a.label}
          </button>
        ))}
      </div>
      {current && (
        <form
          className="grid gap-2 border-t border-[var(--rule)] bg-[var(--cell)] p-3"
          onSubmit={(e) => {
            e.preventDefault()
            void confirm()
          }}
        >
          <p className="m-0 text-[12px] text-[var(--ink-2)]">{current.hint}</p>
          {action === 'edit_approve' && (
            <div className="flex flex-wrap gap-2">
              <label className="flex flex-col gap-1 text-[12px] font-medium">
                Lane
                <select
                  value={lane}
                  onChange={(e) => setLane(e.target.value)}
                  className="rounded-sm border border-[var(--rule)] bg-white px-2 py-[7px]"
                >
                  {LANE_OPTIONS.map((l) => (
                    <option key={l.key} value={l.key}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1 text-[12px] font-medium">
                Suggested initial reserve ($)
                <input
                  value={reserve}
                  onChange={(e) => setReserve(e.target.value)}
                  inputMode="numeric"
                  className="rounded-sm border border-[var(--rule)] bg-white px-2 py-[7px]"
                />
              </label>
            </div>
          )}
          {action === 'send_back' && (
            <fieldset className="m-0 border-0 p-0">
              <legend className="desk-label">Photos to retake</legend>
              <div className="mt-1 flex flex-wrap gap-x-[14px] gap-y-[6px] text-[12px]">
                {photoSlots.map((slot) => (
                  <label key={slot.id} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={slots.includes(slot.id)}
                      onChange={(e) =>
                        setSlots(e.target.checked ? [...slots, slot.id] : slots.filter((s) => s !== slot.id))
                      }
                    />
                    {slot.title}
                  </label>
                ))}
              </div>
            </fieldset>
          )}
          <label className="flex flex-col gap-1 text-[12px] font-medium">
            <span>
              Reason {reasonRequired ? '(required)' : '(optional)'}
              {proposal.flags.length > 0 && action === 'approve' && (
                <span className="font-normal text-[var(--ink-3)]"> — this claim has findings</span>
              )}
            </span>
            <textarea
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
              placeholder="What did you check? e.g. Viewed Wide #2: hood scratches are old wear, not part of this loss."
              className="rounded-sm border border-[var(--rule)] bg-white px-2 py-[7px] font-normal"
            />
          </label>
          {decideError && (
            <p role="alert" className="m-0 text-[12px] text-[var(--bad)]">
              {decideError}
            </p>
          )}
          <div className="flex gap-2">
            <button type="submit" disabled={sending} className="desk-act primary">
              {sending ? 'Confirming…' : `Confirm ${current.label}`}
            </button>
            <button type="button" onClick={() => setAction(null)} className="desk-act">
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  )
}

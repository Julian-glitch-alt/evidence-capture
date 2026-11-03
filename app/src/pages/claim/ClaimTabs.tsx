import { useMemo, useState } from 'react'
import {
  api,
  formatBytes,
  formatDuration,
  formatTime,
  packageUrl,
  reportDraftUrl,
  type ClaimDetail,
  type Damage,
} from '../../api'
import DamageMap, { normalizeArea } from '../../components/DamageMap'
import LinkCard from '../../components/LinkCard'
import Timeline from '../../components/Timeline'
import { checkGroupName, checkPill, damagesForItem, damageTypeWords, photoLabel, slotTitles, usd } from './shared'

export type TabKey = 'damage' | 'photos' | 'checks' | 'notes' | 'timeline' | 'report' | 'file'

const TABS: { key: TabKey; label: (c: ClaimDetail) => string }[] = [
  { key: 'damage', label: (c) => `Damage (${c.damage?.damages?.length ?? 0})` },
  { key: 'photos', label: () => 'Photos' },
  {
    key: 'checks',
    label: (c) => {
      const passed = c.checks.filter((x) => x.result === 'pass').length
      return `Checks ${passed}/${c.checks.length}`
    },
  },
  { key: 'notes', label: (c) => `Notes (${c.review?.notes?.length ?? 0})` },
  { key: 'timeline', label: () => 'Timeline' },
  { key: 'report', label: () => 'Report & delivery' },
  { key: 'file', label: () => 'Claim file' },
]

export default function ClaimTabs({
  claim,
  onUpdate,
  onOpenPhoto,
  selectedArea,
  onSelectArea,
}: {
  claim: ClaimDetail
  onUpdate: (c: ClaimDetail) => void
  onOpenPhoto: (itemId: string) => void
  selectedArea: string | null
  onSelectArea: (a: string | null) => void
}) {
  const [tab, setTab] = useState<TabKey>('damage')
  return (
    <section aria-label="Details">
      <div className="flex flex-wrap gap-[2px] border-b border-[var(--rule)]" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={`bg-none px-3 py-2 text-[12px] font-semibold ${
              tab === t.key
                ? 'border-b-2 border-[var(--ink)] text-[var(--ink)]'
                : 'border-b-2 border-transparent text-[var(--ink-2)]'
            }`}
          >
            {t.label(claim)}
          </button>
        ))}
      </div>
      <div className="pt-[10px]">
        {tab === 'damage' && (
          <DamageTab claim={claim} onUpdate={onUpdate} onOpenPhoto={onOpenPhoto} selectedArea={selectedArea} onSelectArea={onSelectArea} />
        )}
        {tab === 'photos' && <PhotosTab claim={claim} onOpenPhoto={onOpenPhoto} selectedArea={selectedArea} />}
        {tab === 'checks' && <ChecksTab claim={claim} />}
        {tab === 'notes' && <NotesTab claim={claim} onUpdate={onUpdate} />}
        {tab === 'timeline' && <CycleTab claim={claim} />}
        {tab === 'report' && <ReportTab claim={claim} onUpdate={onUpdate} />}
        {tab === 'file' && <FileTab claim={claim} />}
      </div>
    </section>
  )
}

function excludedKeys(claim: ClaimDetail): Set<string> {
  const out = new Set<string>()
  const damages = claim.review?.damages ?? {}
  for (const [key, entry] of Object.entries(damages)) {
    if (entry.verdict === 'excluded') out.add(key)
  }
  return out
}

function declaredAreas(claim: ClaimDetail): string[] {
  const v = claim.answers['damaged_areas']
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string')
  return []
}

function DamageTab({
  claim,
  onUpdate,
  onOpenPhoto,
  selectedArea,
  onSelectArea,
}: {
  claim: ClaimDetail
  onUpdate: (c: ClaimDetail) => void
  onOpenPhoto: (itemId: string) => void
  selectedArea: string | null
  onSelectArea: (a: string | null) => void
}) {
  const excluded = useMemo(() => excludedKeys(claim), [claim])
  const damages = claim.damage?.damages ?? []
  const seenByZone: Record<string, number> = {}
  const markersByZone: Record<string, (string | number)[]> = {}
  damages.forEach((d, i) => {
    if (excluded.has(d.key)) return
    const key = normalizeArea(d.area)
    seenByZone[key] = (seenByZone[key] ?? 0) + 1
    const n = d.number > 0 ? d.number : i + 1
    markersByZone[key] = [...(markersByZone[key] ?? []), n]
  })
  const declared = declaredAreas(claim)
  const filtered = selectedArea ? damages.filter((d) => normalizeArea(d.area) === normalizeArea(selectedArea)) : damages
  const titles = slotTitles(claim)
  const proposal = claim.proposal
  const likely =
    proposal && proposal.status === 'ready' && proposal.estimate.likely
      ? `Likely ${usd(proposal.estimate.likely[0])}–${usd(proposal.estimate.likely[1])}`
      : 'No rough range: the damage photos could not be priced.'

  return (
    <div className="grid gap-4">
      <DamageMap
        seenByZone={seenByZone}
        declaredZones={declared}
        markersByZone={markersByZone}
        selected={selectedArea}
        onSelect={onSelectArea}
      />
      <p className="m-0 text-[12px] text-[var(--ink-2)]">
        {likely}
        {proposal?.status === 'ready' && (
          <> · {proposal.estimate.basis}{proposal.estimate.status === 'incomplete' && ` Not priced: ${proposal.estimate.unpriced.length} item(s).`}</>
        )}
        {selectedArea && (
          <>
            {' '}Showing {filtered.length} of {damages.length} for {selectedArea}.{' '}
            <button type="button" className="underline" onClick={() => onSelectArea(null)}>
              Clear
            </button>
          </>
        )}
      </p>
      <div className="desk-table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Area</th>
              <th>Type</th>
              <th>Severity</th>
              <th>Age</th>
              <th>Photo</th>
              <th>Review</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((d, i) => (
              <DamageRow key={d.key} damage={d} index={i} claim={claim} excluded={excluded.has(d.key)} titles={titles} onUpdate={onUpdate} onOpenPhoto={onOpenPhoto} />
            ))}
          </tbody>
        </table>
      </div>
      {filtered.length === 0 && <p className="text-[12px] text-[var(--ink-3)]">No damage in this view.</p>}
    </div>
  )
}

function DamageRow({
  damage,
  index,
  claim,
  excluded,
  titles,
  onUpdate,
  onOpenPhoto,
}: {
  damage: Damage
  index: number
  claim: ClaimDetail
  excluded: boolean
  titles: Record<string, string>
  onUpdate: (c: ClaimDetail) => void
  onOpenPhoto: (itemId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const entry = claim.review?.damages?.[damage.key]
  const n = damage.number > 0 ? damage.number : index + 1
  const photoLabelText = titles[damage.slot_id] ?? damage.slot_id

  const save = async (verdict: 'excluded' | 'included') => {
    if (verdict === 'excluded' && reason.trim().length < 5) {
      setError('A reason of 5–500 characters is required to take damage out of this loss.')
      return
    }
    setSaving(true)
    setError(undefined)
    try {
      const body =
        verdict === 'excluded'
          ? { kind: 'damage' as const, key: damage.key, verdict, reason: reason.trim() }
          : { kind: 'damage' as const, key: damage.key, verdict }
      onUpdate(await api.review(claim.id, body))
      setOpen(false)
      setReason('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the review')
    } finally {
      setSaving(false)
    }
  }

  return (
    <tr className={excluded ? 'text-[var(--ink-3)]' : undefined}>
      <td>{n}</td>
      <td className={excluded ? 'line-through' : undefined}>
        {damage.area}
        {damage.area_read && (
          <span className="block text-[11px] font-normal text-[var(--ink-3)]">
            Side worked out from the photo angle (the model said {damage.area_read}).
          </span>
        )}
      </td>
      <td className={excluded ? 'line-through' : undefined}>{damageTypeWords(damage.damage_type)}</td>
      <td>{damage.severity}</td>
      <td>{damage.age === 'old' ? 'Possibly old' : damage.age === 'new' ? 'New' : 'Unsure'}</td>
      <td>
        <button type="button" className="underline" onClick={() => onOpenPhoto(damage.item_id)}>
          {photoLabelText}
        </button>
      </td>
      <td>
        {excluded && entry ? (
          <span className="text-[12px]">
            Excluded: {entry.reason ?? ''} · {entry.by}{' '}
            {!claim.decision && (
              <button type="button" className="underline" onClick={() => void save('included')}>
                Include again
              </button>
            )}
          </span>
        ) : !claim.decision ? (
          open ? (
            <span className="grid gap-1">
              <label className="grid gap-1 text-[12px]">
                Why is this not part of this loss?
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="e.g. Hood scratches are old wear"
                  className="rounded-sm border border-[var(--rule)] bg-white px-2 py-1"
                />
              </label>
              {error && (
                <span role="alert" className="text-[12px] text-[var(--bad)]">
                  {error}
                </span>
              )}
              <span className="flex gap-2">
                <button type="button" className="desk-act" disabled={saving} onClick={() => void save('excluded')}>
                  {saving ? 'Saving…' : 'Exclude'}
                </button>
                <button type="button" className="desk-act" onClick={() => setOpen(false)}>
                  Cancel
                </button>
              </span>
            </span>
          ) : (
            <button type="button" className="desk-act" onClick={() => setOpen(true)}>
              Not part of this loss
            </button>
          )
        ) : (
          <span className="text-[12px] text-[var(--ink-3)]">Locked</span>
        )}
      </td>
    </tr>
  )
}

function PhotosTab({
  claim,
  onOpenPhoto,
  selectedArea,
}: {
  claim: ClaimDetail
  onOpenPhoto: (itemId: string) => void
  selectedArea: string | null
}) {
  const current = claim.items.filter((i) => !i.superseded && i.kind !== 'video')
  const byArea = selectedArea
    ? current.filter((item) => damagesForItem(claim, item.id).some((d) => normalizeArea(d.area) === normalizeArea(selectedArea)))
    : current
  if (current.length === 0) return <p className="text-[12px] text-[var(--ink-3)]">Nothing captured yet.</p>
  return (
    <div>
      {selectedArea && (
        <p className="mt-0 text-[12px] text-[var(--ink-2)]">
          Filtered to {selectedArea}: {byArea.length} of {current.length} photos.
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {byArea.map((item) => {
          const label = photoLabel(claim, item)
          const damages = damagesForItem(claim, item.id)
          if (item.removed_at) {
            return (
              <figure key={item.id} className="m-0 overflow-hidden rounded-sm border border-[var(--rule)] bg-white">
                <div className="grid aspect-[4/3] w-full place-items-center bg-[var(--cell)] text-[12px] text-[var(--ink-3)]">
                  Removed
                </div>
                <figcaption className="space-y-1 p-2 text-[11px] text-[var(--ink-2)]">
                  <p className="m-0 font-medium text-[var(--ink)]">{label}</p>
                  <p className="m-0">Removed {formatTime(item.removed_at ?? null)}</p>
                </figcaption>
              </figure>
            )
          }
          return (
            <figure key={item.id} className="m-0 overflow-hidden rounded-sm border border-[var(--rule)] bg-white">
              <button
                type="button"
                onClick={() => onOpenPhoto(item.id)}
                className="relative block w-full cursor-zoom-in border-0 bg-[var(--cell)] p-0"
                aria-label={`Open photo ${label}`}
              >
                <img src={api.fileUrl(item.id)} alt={label} loading="lazy" className="aspect-[4/3] w-full object-cover" />
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
                      <i>{d.number > 0 ? d.number : i + 1}</i>
                    </span>
                  ) : null,
                )}
              </button>
              <figcaption className="space-y-1 p-2 text-[11px] text-[var(--ink-2)]">
                <p className="m-0 font-medium text-[var(--ink)]">{label}</p>
                <p className="m-0">
                  Received {formatTime(item.received_at)} · {formatBytes(item.size)}
                </p>
              </figcaption>
            </figure>
          )
        })}
      </div>
    </div>
  )
}

function ChecksTab({ claim }: { claim: ClaimDetail }) {
  const groups: ('Coverage' | 'Photos' | 'Statement vs photos')[] = ['Coverage', 'Photos', 'Statement vs photos']
  const rank = (r: string) => (r === 'fail' ? 0 : r === 'unclear' ? 1 : 2)
  const passed = claim.checks.filter((c) => c.result === 'pass').length
  return (
    <div className="grid gap-4">
      <p className="m-0 text-[12px] text-[var(--ink-2)]">
        {passed}/{claim.checks.length} passed.
      </p>
      {groups.map((g) => {
        const rows = claim.checks
          .filter((c) => checkGroupName(c.check_id) === g)
          .sort((a, b) => rank(a.result) - rank(b.result))
        if (rows.length === 0) return null
        return (
          <div key={g}>
            <h3 className="desk-label mb-1">{g}</h3>
            <div className="desk-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th>Result</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={c.check_id}>
                      <td>{c.check_id}</td>
                      <td>{checkPill(c.result)}</td>
                      <td className="text-[var(--ink-2)]">{c.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function NotesTab({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const notes = claim.review?.notes ?? []
  const save = async () => {
    const v = text.trim()
    if (v.length < 5 || v.length > 1000) {
      setError('Notes are 5–1000 characters.')
      return
    }
    setSaving(true)
    setError(undefined)
    try {
      onUpdate(await api.review(claim.id, { kind: 'note', text: v }))
      setText('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not add the note')
    } finally {
      setSaving(false)
    }
  }
  return (
    <div className="grid gap-3">
      {notes.length === 0 && <p className="text-[12px] text-[var(--ink-3)]">No review notes yet.</p>}
      <ul className="m-0 grid list-none gap-2 p-0">
        {notes.map((n, i) => (
          <li key={i} className="rounded-sm bg-[var(--cell)] px-3 py-2 text-[12.5px]">
            <p className="m-0">{n.text}</p>
            <p className="m-0 mt-1 text-[11.5px] text-[var(--ink-3)]">
              {n.by} · {formatTime(n.at)}
            </p>
          </li>
        ))}
      </ul>
      {!claim.decision && (
        <form
          className="grid gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
          <label className="grid gap-1 text-[12px] font-medium">
            Add a review note
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={2}
              placeholder="What should the next adjuster know?"
              className="rounded-sm border border-[var(--rule)] bg-white px-2 py-[7px] font-normal"
            />
          </label>
          {error && (
            <p role="alert" className="m-0 text-[12px] text-[var(--bad)]">
              {error}
            </p>
          )}
          <div>
            <button type="submit" disabled={saving} className="desk-act">
              {saving ? 'Adding…' : 'Add note'}
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

function CycleTab({ claim }: { claim: ClaimDetail }) {
  const stages: { label: string; value: string; done: boolean }[] = [
    { label: 'Link sent', value: formatTime(claim.created_at), done: true },
    {
      label: 'Photos in',
      value: claim.submitted_at ? `+ ${formatDuration(claim.cycle.link_to_submit)}` : 'waiting',
      done: claim.submitted_at != null,
    },
    {
      label: 'Report ready',
      value: claim.report ? `+ ${formatDuration(claim.cycle.submit_to_report)}` : 'waiting',
      done: claim.report != null,
    },
    {
      label: 'Decision',
      value: claim.decision ? formatTime(claim.decision.decided_at) : 'waiting',
      done: claim.decision != null,
    },
  ]
  return (
    <div>
      <div className="desk-tl">
        {stages.map((s) => (
          <div key={s.label} className={s.done ? 'done' : ''}>
            <span className="desk-label">{s.label}</span>
            <b>{s.value}</b>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[12px] text-[var(--ink-2)]">
        Link → submit {formatDuration(claim.cycle.link_to_submit)}
        {' · '}submit → report {formatDuration(claim.cycle.submit_to_report)}
        {' · '}report → decision {formatDuration(claim.cycle.report_to_decision)}
      </p>
    </div>
  )
}

function ReportTab({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string>()
  const report = claim.report
  if (!report) return <p className="text-[12px] text-[var(--ink-3)]">The report is prepared when the analysis finishes.</p>
  const resend = async () => {
    setSending(true)
    setError(undefined)
    try {
      await api.deliver(claim.id)
      onUpdate(await api.claim(claim.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Resend failed')
    } finally {
      setSending(false)
    }
  }
  const ai = claim.ai_summary
  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap gap-2">
        <a href={reportDraftUrl(report.url)} target="_blank" rel="noreferrer" className="desk-act primary">
          Open draft (PDF)
        </a>
        {report.reviewed_sha256 ? (
          <a href={report.url} target="_blank" rel="noreferrer" className="desk-act">
            Open reviewed (PDF)
          </a>
        ) : (
          <span className="self-center text-[12px] text-[var(--ink-3)]">Reviewed report appears here after the decision.</span>
        )}
        <a href={packageUrl(claim.id)} className="desk-act">
          Download sealed package
        </a>
      </div>
      <p className="m-0 truncate font-mono text-[11px] text-[var(--ink-3)]" title={report.sha256}>
        Draft sha256 {report.sha256}
      </p>
      {report.reviewed_sha256 && (
        <p className="m-0 truncate font-mono text-[11px] text-[var(--ink-3)]" title={report.reviewed_sha256}>
          Reviewed sha256 {report.reviewed_sha256}
          {report.reviewed_at && ` · ${formatTime(report.reviewed_at)}`}
        </p>
      )}
      <div className="grid gap-2">
        {report.outbox.map((m) => (
          <p key={m.id} className="m-0 text-[12px] text-[var(--ink-2)]">
            {String(m.event)} ·{' '}
            {m.delivered_at ? `delivered ${formatTime(m.delivered_at)}` : `not delivered: ${m.last_error ?? 'pending'} (${m.attempts} attempts)`}
          </p>
        ))}
        <div className="flex items-center gap-2">
          <button type="button" onClick={() => void resend()} disabled={sending} className="desk-act">
            {sending ? 'Resending…' : 'Resend'}
          </button>
          {error && <span className="text-[12px] text-[var(--bad)]">{error}</span>}
        </div>
      </div>
      <div className="rounded-sm bg-[var(--cell)] p-3 text-[12px]">
        {ai == null ? (
          <p className="m-0">No AI summary for this claim.</p>
        ) : ai.status === 'verified' ? (
          <p className="m-0">AI summary passed the sentence checks ({ai.sentences.length} sentences). Shown beside the summary.</p>
        ) : (
          <>
            <p className="m-0 font-semibold">AI summary {ai.status}: not shown in the workspace.</p>
            <ul className="m-1 pl-4">
              {ai.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  )
}

function FileTab({ claim }: { claim: ClaimDetail }) {
  const p = claim.policy.data
  return (
    <div className="grid gap-6">
      <section>
        <h3 className="desk-label mb-2">Policy snapshot</h3>
        {!p ? (
          <div className="rounded-sm border border-[var(--warn)] bg-[var(--warn-bg)] p-4 text-[13px]">
            <p className="m-0 font-medium">
              {claim.policy.policy_no} was not found in the policy system.
            </p>
            <p className="m-0 mt-1">The claim can collect evidence, but it cannot reach payment until the policy is verified.</p>
          </div>
        ) : (
          <dl className="grid grid-cols-2 gap-4 rounded-sm border border-[var(--rule)] bg-white p-4 text-[13px]">
            <div>
              <dt className="text-[var(--ink-3)]">Policy</dt>
              <dd className="font-medium">
                {claim.policy.policy_no} · {p.state}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--ink-3)]">Period</dt>
              <dd className="font-medium">
                {p.effective} → {p.expires}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--ink-3)]">Holder</dt>
              <dd className="font-medium">{p.holder.name}</dd>
            </div>
            <div>
              <dt className="text-[var(--ink-3)]">Drivers</dt>
              <dd className="font-medium">{p.drivers.map((d) => d.name).join(', ')}</dd>
            </div>
            <div>
              <dt className="text-[var(--ink-3)]">Vehicle</dt>
              <dd className="font-medium">
                {p.vehicles[0] ? `${p.vehicles[0].year} ${p.vehicles[0].make} ${p.vehicles[0].model}` : '—'}
              </dd>
            </div>
            <div>
              <dt className="text-[var(--ink-3)]">Plate · VIN</dt>
              <dd className="font-medium">
                {p.vehicles[0] ? `${p.vehicles[0].plate} · ${p.vehicles[0].vin}` : '—'}
              </dd>
            </div>
          </dl>
        )}
      </section>
      {['opened', 'capturing'].includes(claim.status) && (
        <section>
          <h3 className="desk-label mb-2">Capture link</h3>
          <LinkCard token={claim.token} claimId={claim.id} />
        </section>
      )}
      <section>
        <h3 className="desk-label mb-2">Sessions</h3>
        {claim.sessions.length === 0 ? (
          <p className="text-[12px] text-[var(--ink-3)]">No capture sessions yet.</p>
        ) : (
          <ul className="m-0 grid list-none gap-1 p-0 text-[12px]">
            {claim.sessions.map((s, i) => (
              <li key={i}>
                Code <span className="font-mono font-semibold">{s.code}</span> · started {formatTime(s.started_at)} · expires{' '}
                {formatTime(s.expires_at)}
                {s.superseded ? ' · superseded' : ''}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h3 className="desk-label mb-2">Statement answers</h3>
        {claim.layout.sections
          .filter((s) => s.id !== 'policy')
          .map((section) => (
            <div key={section.id} className="mb-3 rounded-sm border border-[var(--rule)]">
              <h4 className="m-0 border-b border-[var(--rule)] px-3 py-2 text-[13px] font-medium">
                {section.number}. {section.title}
              </h4>
              {section.fields.length > 0 && (
                <dl className="m-0 divide-y divide-slate-100">
                  {section.fields.map((f) => {
                    const v = claim.answers[f.id]
                    return (
                      <div key={f.id} className="grid gap-1 px-3 py-2 text-[12.5px] sm:grid-cols-3">
                        <dt className="text-[var(--ink-3)]">{f.label}</dt>
                        <dd className="m-0 whitespace-pre-wrap sm:col-span-2">
                          {Array.isArray(v) ? v.join(', ') : v === true ? 'Yes' : typeof v === 'string' && v ? v : '—'}
                        </dd>
                      </div>
                    )
                  })}
                </dl>
              )}
            </div>
          ))}
      </section>
      <section>
        <h3 className="desk-label mb-2">Event log</h3>
        <Timeline events={claim.events} />
      </section>
    </div>
  )
}

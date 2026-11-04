import { Link } from 'react-router-dom'
import { api, formatTime, type ClaimDetail } from '../api'
import LinkCard from './LinkCard'
import { normalizeArea } from './DamageMap'
import { formatAnswer, holderOf, shortHash, vehicleOf, type OrderedFinding } from '../pages/claim/shared'

function answered(value: unknown): boolean {
  if (value === null || value === undefined) return false
  if (typeof value === 'boolean') return true
  if (Array.isArray(value)) return value.some((v) => typeof v === 'string' && v.trim() !== '')
  if (typeof value === 'string') return value.trim() !== ''
  return false
}

/** T59: an answer that is the escape option reads with the claimant's own words
 *  after a colon ("Scrape or other: A cart rolled into the door"). */
function answerWithOwnWords(answers: Record<string, unknown>, id: string, other: string | undefined, text: string): string {
  if (other == null) return text
  const words = answers[`${id}_other`]
  if (typeof words !== 'string' || words.trim() === '') return text
  const value = answers[id]
  const selected = Array.isArray(value) ? value.includes(other) : value === other
  return selected ? `${text}: ${words.trim()}` : text
}

/** Caption time: only the time when the photo and the submission share a day. */
function captionWhen(receivedAt: string, submittedAt: string | null): string {
  if (submittedAt) {
    const r = new Date(receivedAt)
    const s = new Date(submittedAt)
    if (r.getFullYear() === s.getFullYear() && r.getMonth() === s.getMonth() && r.getDate() === s.getDate()) {
      return r.toLocaleString(undefined, { timeStyle: 'short' })
    }
  }
  return formatTime(receivedAt)
}

export default function LossNotice({
  claim,
  findings,
  selectedArea,
  onOpenPhoto,
}: {
  claim: ClaimDetail
  findings: OrderedFinding[]
  selectedArea: string | null
  onOpenPhoto: (itemId: string) => void
}) {
  if (!claim.submitted_at) {
    return (
      <div className="ev-desk">
        <article className="ev-page" aria-label="Loss notice as submitted by the claimant">
          <div className="ev-runhead">
            <span>Loss notice</span>
            <span>Claim {claim.id}</span>
          </div>
          <h2 className="ev-wtitle">Automobile Loss Notice</h2>
          <p className="ev-wsub">
            The link was sent {formatTime(claim.created_at)} and the claimant has not submitted yet. The claim
            moves to “Needs me” as soon as the photos arrive.
          </p>
          <section className="ev-wsec">
            <LinkCard token={claim.token} claimId={claim.id} />
          </section>
          <div className="ev-runfoot">
            <span>Form DM-LN-01, page 1 of 1</span>
          </div>
        </article>
      </div>
    )
  }

  const fieldPin = new Map<string, { n: number; level: string }>()
  for (const f of findings) {
    for (const field of f.entry.fields ?? []) {
      if (!fieldPin.has(field)) fieldPin.set(field, { n: f.n, level: f.entry.level })
    }
  }
  const itemPin = new Map<string, { n: number; level: string }>()
  for (const f of findings) {
    const first = f.entry.evidence[0]
    if (first && !itemPin.has(first.item_id)) itemPin.set(first.item_id, { n: f.n, level: f.entry.level })
  }

  const titles: Record<string, string> = {}
  for (const section of claim.layout.sections) {
    for (const slot of section.slots) titles[slot.id] = slot.title
  }

  const holder = holderOf(claim)
  const vehicle = vehicleOf(claim)
  const figures = claim.items.filter((i) => !i.superseded && i.kind !== 'signature')
  const signature = claim.items.find((i) => !i.superseded && i.kind === 'signature')
  const signer = claim.contact?.name ?? (holder === '—' ? 'The claimant' : holder)
  const damagesByItem = new Map<string, string[]>()
  for (const d of claim.damage?.damages ?? []) {
    const list = damagesByItem.get(d.item_id) ?? []
    list.push(d.area)
    damagesByItem.set(d.item_id, list)
  }

  // Numbered in display order (1, 2, 3, ...). The signing section is not a table:
  // its answers live in the final Declaration section instead.
  const sections = claim.layout.sections.filter(
    (s) => s.id !== 'declaration' && s.fields.some((f) => answered(claim.answers[f.id])),
  )
  const signingFields = (claim.layout.sections.find((s) => s.id === 'declaration')?.fields ?? []).filter((f) =>
    answered(claim.answers[f.id]),
  )

  return (
    <div className="ev-desk">
      <article className="ev-page" aria-label="Loss notice as submitted by the claimant">
        <div className="ev-runhead">
          <span>Loss notice</span>
          <span>Claim {claim.id}</span>
        </div>
        <h2 className="ev-wtitle">Automobile Loss Notice</h2>
        <p className="ev-wsub">
          Submitted by {holder} through the secure capture link on {formatTime(claim.submitted_at)}. Each answer
          is shown under the question the claimant was asked.
        </p>

        {sections.map((section, i) => (
          <section key={section.id} className="ev-wsec">
            <h3>
              {i + 1}.&ensp;{section.title}
            </h3>
            <table className="ev-wtable">
              <tbody>
                {section.fields
                  .filter((f) => answered(claim.answers[f.id]))
                  .map((f) => {
                    const pin = fieldPin.get(f.id)
                    const text = answerWithOwnWords(claim.answers, f.id, f.other, formatAnswer(claim.answers[f.id]))
                    return (
                      <tr key={f.id} id={pin ? `answer-${f.id}` : undefined}>
                        <th scope="row">{f.label}</th>
                        <td>
                          {pin ? (
                            <>
                              <mark className={pin.level === 'review' ? 'review' : pin.level === 'note' ? 'note' : undefined}>
                                {text}
                              </mark>
                              <sup className={`ev-cref ${pin.level === 'review' ? 'review' : pin.level === 'note' ? 'note' : ''}`}>
                                {pin.n}
                              </sup>
                            </>
                          ) : (
                            text
                          )}
                        </td>
                      </tr>
                    )
                  })}
              </tbody>
            </table>
          </section>
        ))}

        {figures.length > 0 && (
          <section className="ev-wsec">
            <h3>
              {sections.length + 1}.&ensp;Photos and video
            </h3>
            <div className="ev-wfigs">
              {figures.map((item, i) => {
                const pin = itemPin.get(item.id)
                const title = titles[item.step_id] ?? item.step_id
                const highlighted =
                  selectedArea != null &&
                  (damagesByItem.get(item.id) ?? []).some(
                    (a) => normalizeArea(a) === normalizeArea(selectedArea),
                  )
                const itemFlags = item.flags ?? []
                const flagNotes: string[] = []
                if (itemFlags.includes('static_source')) flagNotes.push('static source — liveness frames missing or near-identical')
                if (itemFlags.includes('slow_upload')) flagNotes.push('slow upload — arrived more than 60 s after the capture code was issued')
                const caption = (
                  <figcaption>
                    {flagNotes.length > 0 ? (
                      <>Figure {i + 1}. {title} — flagged: {flagNotes.join('; ')}.</>
                    ) : (
                      <>Figure {i + 1}. {title}, captured in the capture flow at {captionWhen(item.received_at, claim.submitted_at)}.</>
                    )}
                    {pin && (
                      <sup className={`ev-cref ${pin.level === 'review' ? 'review' : pin.level === 'note' ? 'note' : ''}`}>
                        {pin.n}
                      </sup>
                    )}
                  </figcaption>
                )
                if (item.kind === 'video') {
                  return (
                    <figure
                      key={item.id}
                      className={`ev-wfig${pin ? ` pin${pin.level === 'review' ? ' review' : ''}` : ''}`}
                      id={`figure-${item.id}`}
                    >
                      <a
                        href={api.fileUrl(item.id)}
                        target="_blank"
                        rel="noreferrer"
                        className="ev-wfig-media press"
                        aria-label={`Open ${title}`}
                        style={highlighted ? { outline: '2.5px solid var(--brand)', outlineOffset: 2 } : undefined}
                      >
                        <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                          <circle cx="12" cy="12" r="9" />
                          <path d="M10 8.5l6 3.5-6 3.5z" fill="currentColor" stroke="none" />
                        </svg>
                      </a>
                      {caption}
                    </figure>
                  )
                }
                return (
                  <figure
                    key={item.id}
                    className={`ev-wfig${pin ? ` pin${pin.level === 'review' ? ' review' : ''}` : ''}`}
                    id={`figure-${item.id}`}
                  >
                    <button
                      type="button"
                      className="press"
                      onClick={() => onOpenPhoto(item.id)}
                      aria-label={`Open ${title}`}
                      style={highlighted ? { outline: '2.5px solid var(--brand)', outlineOffset: 2 } : undefined}
                    >
                      <img src={api.fileUrl(item.id)} alt="" loading="lazy" />
                    </button>
                    {caption}
                  </figure>
                )
              })}
            </div>
          </section>
        )}

        <section className="ev-wsec">
          <h3>
            {sections.length + (figures.length > 0 ? 2 : 1)}.&ensp;Declaration
          </h3>
          <p className="ev-wdecl">
            I declare that the information in this notice is true and complete to the best of my knowledge, and
            that the photos and video were taken of the insured vehicle at the time stated.
          </p>
          {signingFields.length > 0 && (
            <table className="ev-wtable">
              <tbody>
                {signingFields.map((f) => (
                  <tr key={f.id}>
                    <th scope="row">{f.label}</th>
                    <td>{formatAnswer(claim.answers[f.id])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="ev-wsign">
            <div>
              <div className="sig">
                {signature && <img src={api.fileUrl(signature.id)} alt="Claimant signature" />}
              </div>
              <div className="sigline">
                <b>{signer}</b>
                <br />
                Signed on the claimant&apos;s phone, {claim.seal ? formatTime(claim.seal.sealed_at) : formatTime(claim.submitted_at)}
              </div>
            </div>
            <div className="ev-stamp" aria-label="Sealed">
              <span>
                <b>SEALED</b>
                {claim.seal ? formatTime(claim.seal.sealed_at).split(',')[0] : ''}
                <br />
                Ed25519
              </span>
            </div>
          </div>
        </section>

        <div className="ev-runfoot">
          <span>Form DM-LN-01, page 1 of 1</span>
          <span>
            Seal{' '}
            <span className="mono">{claim.seal ? shortHash(claim.seal.manifest_sha256) : '—'}</span>, check it on
            the <Link to="/verify">Verify page</Link>
          </span>
        </div>
        <p className="mute" style={{ fontSize: 11.5, marginTop: 8 }}>
          {vehicle} · Policy {claim.policy.policy_no}
          {claim.seal ? ` · Seal key ${claim.seal.key_id}` : ''}
        </p>
      </article>
    </div>
  )
}

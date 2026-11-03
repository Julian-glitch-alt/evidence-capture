import { useState } from 'react'
import type { Answer, ClaimantPolicy, Field, Item, Slot } from '../../api'
import CarDiagram from '../../components/CarDiagram'

export function FieldInput({
  field,
  value,
  otherValue,
  onChange,
  onOtherChange,
}: {
  field: Field
  value: Answer | undefined
  /** T59: the live `<id>_other` text; falls back to the layout's `other_text`. */
  otherValue?: string
  onChange: (v: Answer) => void
  onOtherChange?: (v: string) => void
}) {
  const id = `f-${field.id}`
  /** T59: the escape option is chosen, so the claimant writes past it in their own words. */
  const otherSelected =
    field.other != null && (Array.isArray(value) ? value.includes(field.other) : value === field.other)
  if (field.type === 'checkbox') {
    return (
      <div className="t45-field mb-1">
        <label className="flex gap-3 text-[15px]">
          <input id={id} type="checkbox" className="mt-0.5 h-5 w-5" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
          {field.label}
        </label>
        {field.error && <p className="t45-err mt-1">{field.error}</p>}
      </div>
    )
  }
  const label = (
    <label htmlFor={id} id={`${id}-label`}>
      {field.label}
      {!field.required && <span className="font-normal text-[var(--mute)]"> (optional)</span>}
    </label>
  )
  let control
  if (field.type === 'choice' && (field.options?.length ?? 0) <= 6) {
    control = (
      <div role="radiogroup" aria-labelledby={`${id}-label`} className="flex flex-wrap gap-2">
        {field.options!.map((o) => (
          <button
            key={o}
            type="button"
            role="radio"
            aria-checked={value === o}
            onClick={() => onChange(o)}
            className={`t45-choice${value === o ? ' on' : ''}`}
          >
            {o}
          </button>
        ))}
      </div>
    )
  } else if (field.type === 'choice') {
    control = (
      <select id={id} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose…</option>
        {field.options!.map((o) => (
          <option key={o}>{o}</option>
        ))}
      </select>
    )
  } else if (field.type === 'multiselect') {
    const selected = Array.isArray(value) ? value : []
    control = (
      <div role="group" aria-labelledby={`${id}-label`} className="flex flex-wrap gap-2">
        {field.options!.map((o) => {
          const pressed = selected.includes(o)
          return (
            <button
              key={o}
              type="button"
              aria-pressed={pressed}
              onClick={() => onChange(pressed ? selected.filter((x) => x !== o) : [...selected, o])}
              className={`t45-choice${pressed ? ' on' : ''}`}
            >
              {o}
            </button>
          )
        })}
      </div>
    )
  } else if (field.type === 'textarea') {
    control = <textarea id={id} rows={4} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} />
  } else {
    const type = field.type === 'vin' ? 'text' : field.type === 'number' ? 'text' : field.type
    control = (
      <input
        id={id}
        type={type}
        inputMode={field.type === 'number' ? 'numeric' : undefined}
        autoCapitalize={field.type === 'vin' ? 'characters' : undefined}
        autoComplete="off"
        className={field.type === 'vin' ? 'font-semibold tracking-wider uppercase' : undefined}
        value={(value as string) ?? ''}
        onChange={(e) => onChange(field.type === 'vin' ? e.target.value.toUpperCase() : e.target.value)}
      />
    )
  }
  return (
    <div className="t45-field mb-1">
      {label}
      {field.hint && <p className="t45-help mb-2">{field.hint}</p>}
      {control}
      {otherSelected && (
        <textarea
          aria-label={`${field.label} (in your own words)`}
          placeholder="Tell us in your own words"
          rows={2}
          maxLength={200}
          value={otherValue ?? field.other_text ?? ''}
          onChange={(e) => onOtherChange?.(e.target.value)}
        />
      )}
      {field.error && <p className="t45-err mt-1">{field.error}</p>}
      {!field.error && field.note && <p className="t45-note mt-1">{field.note}</p>}
    </div>
  )
}

export function SlotTile({
  slot,
  items,
  fileUrl,
  onOpen,
  onRemove,
  skipReasons,
  onSkip,
  number,
  next = false,
  done: doneOverride,
  mode = 'capture',
}: {
  slot: Slot
  items: Item[]
  fileUrl: (itemId: string) => string
  onOpen?: () => void
  onRemove?: (id: string) => void
  skipReasons?: string[]
  onSkip?: (skip: { reason: string; note?: string } | null) => void
  /** Position among the shots asked for (1-based). */
  number?: number
  /** The next shot to take: drawn a little louder. */
  next?: boolean
  /** Round 2 decides done by shots taken since the Send back, not by any shot. */
  done?: boolean
  /** 'retake': asked for again after a Send back; 'kept': read-only, as sent in round 1. */
  mode?: 'capture' | 'retake' | 'kept'
}) {
  const [asking, setAsking] = useState(false)
  const [note, setNote] = useState('')
  const done = doneOverride ?? items.length > 0
  const max = slot.max_items ?? 1
  const first = items[0]
  const canAdd = mode !== 'kept' && (slot.multiple ? items.length < max : true)
  const pill =
    slot.skipped != null ? (
      <span className="inline-flex items-center rounded-full bg-[#FDF3DC] px-2.5 py-1 text-xs font-bold text-[#5C3D00]">
        Skipped: {slot.skipped.reason}
      </span>
    ) : mode === 'kept' ? (
      <span className="inline-flex items-center rounded-full border border-[var(--line)] px-2.5 py-1 text-xs font-bold text-[var(--mute)]">
        Kept as you sent it
      </span>
    ) : mode === 'retake' && !done ? (
      <span className="inline-flex items-center rounded-full bg-[var(--wash)] px-2.5 py-1 text-xs font-bold text-[var(--brand-deep)]">
        Requested again
      </span>
    ) : done ? (
      <span className="inline-flex items-center gap-1 rounded-full bg-[#E9F3EE] px-2.5 py-1 text-xs font-bold text-[#00694A]">
        ✓ Taken{slot.multiple ? ` ${items.length} of ${max}` : ''}
      </span>
    ) : slot.required ? (
      <span className="inline-flex items-center rounded-full bg-[var(--wash)] px-2.5 py-1 text-xs font-bold text-[var(--brand-deep)]">
        Required
      </span>
    ) : (
      <span className="inline-flex items-center rounded-full border border-[var(--line)] px-2.5 py-1 text-xs font-bold text-[var(--mute)]">
        Optional
      </span>
    )
  return (
    <div className="rounded-[10px] border border-[var(--line)] bg-white p-3">
      <div className="flex items-start gap-3">
        {done && first ? (
          <div className="relative h-16 w-16 flex-none overflow-hidden rounded-[10px] bg-[var(--wash)]">
            {first.kind === 'video' ? (
              <div className="flex h-full items-center justify-center bg-[#2a3036] text-xs text-white">Video</div>
            ) : (
              <img src={fileUrl(first.id)} alt="" className="h-full w-full object-cover" />
            )}
            {slot.multiple && onRemove && mode !== 'kept' && (
              <button onClick={() => onRemove(first.id)} aria-label="Remove photo" className="absolute right-0.5 top-0.5 rounded-full bg-black/70 px-1.5 text-xs text-white">
                ✕
              </button>
            )}
          </div>
        ) : (
          number !== undefined && (
            <span className="flex h-8 w-8 flex-none items-center justify-center rounded-full bg-[var(--wash)] text-sm font-bold text-[var(--brand-deep)]">
              {number}
            </span>
          )
        )}
        <div className="min-w-0 flex-1">
          <p className="text-[15px] font-bold leading-snug text-[var(--text)]">
            {slot.title}
            {!slot.required && <span className="font-normal text-[var(--mute)]"> (optional)</span>}
            {next && !done && <span className="ml-2 rounded bg-[var(--brand)] px-1.5 py-0.5 align-middle text-[11px] font-bold uppercase tracking-wide text-white">Next</span>}
          </p>
          {!done && mode !== 'kept' && slot.instructions && <p className="mt-0.5 text-[13px] leading-snug text-[var(--mute)]">{slot.instructions}</p>}
          <div className="mt-1.5">{pill}</div>
        </div>
      </div>
      {done && slot.multiple && items.length > 1 && (
        <div className="mt-2 flex gap-2 overflow-x-auto">
          {items.slice(1).map((item) => (
            <div key={item.id} className="relative h-16 w-16 flex-none overflow-hidden rounded-md bg-slate-200">
              {item.kind === 'video' ? (
                <div className="flex h-full items-center justify-center bg-[#2a3036] text-xs text-white">Video</div>
              ) : (
                <img src={fileUrl(item.id)} alt="" className="h-full w-full object-cover" />
              )}
              {slot.multiple && onRemove && mode !== 'kept' && (
                <button onClick={() => onRemove(item.id)} aria-label="Remove photo" className="absolute right-0.5 top-0.5 rounded-full bg-black/70 px-1.5 text-xs text-white">
                  ✕
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      {!done && slot.skipped && (
        <div className="mt-2 rounded-lg bg-[#FDF3DC] px-3 py-2 text-sm text-[#5C3D00]">
          Not taken: {slot.skipped.reason}
          {slot.skipped.note ? ` (${slot.skipped.note})` : ''}. An adjuster may ask for it later.
          <button onClick={() => onSkip?.(null)} className="ml-2 font-semibold underline">
            Undo
          </button>
        </div>
      )}
      {canAdd && onOpen && (
        <button
          data-testid={`shoot-${slot.id}`}
          onClick={onOpen}
          className="mt-3 w-full rounded-[10px] bg-[var(--brand)] py-3.5 text-base font-semibold text-white"
        >
          {done ? (slot.multiple ? 'Add another' : 'Retake') : slot.kind === 'video' ? 'Record video' : 'Take photo'}
        </button>
      )}
      {mode === 'capture' && onSkip && !done && !slot.skipped && slot.required && slot.skippable && !asking && (
        <button onClick={() => setAsking(true)} className="mt-1 min-h-11 w-full text-center text-sm text-[var(--mute)] underline">
          Skip with a reason
        </button>
      )}
      {asking && !slot.skipped && (
        <div className="mt-3 space-y-2 rounded-lg border border-[var(--line)] bg-[var(--page)] p-3">
          <p className="text-sm font-semibold">Why not?</p>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Anything we should know (optional)"
            aria-label="Note about the missing shot"
            className="w-full rounded-lg border border-[var(--line)] bg-white px-3 py-2 text-sm"
          />
          <div className="flex flex-wrap gap-2">
            {(skipReasons ?? []).map((r) => (
              <button
                key={r}
                onClick={() => {
                  onSkip?.({ reason: r, ...(note.trim() ? { note: note.trim() } : {}) })
                  setAsking(false)
                }}
                className="min-h-11 rounded-full border border-[var(--line)] bg-white px-3.5 py-2 text-sm"
              >
                {r}
              </button>
            ))}
          </div>
          <button onClick={() => setAsking(false)} className="text-sm text-[var(--mute)] underline">
            Cancel
          </button>
        </div>
      )}
    </div>
  )
}

/** T45 damaged-parts screen: the approved car illustration plus one chip per area.
 *  "Interior" reads "Inside the car" on its chip; the option values stay as the server knows them. */
export function PartsField({ field, value, onChange }: { field: Field; value: string[]; onChange: (v: string[]) => void }) {
  const toggle = (o: string) => onChange(value.includes(o) ? value.filter((x) => x !== o) : [...value, o])
  const chipLabel = (o: string) => (o === 'Interior' ? 'Inside the car' : o)
  return (
    <div>
      <CarDiagram selected={value} />
      <div className="mt-2 flex flex-wrap gap-2">
        {(field.options ?? []).map((o) => {
          const pressed = value.includes(o)
          return (
            <button
              key={o}
              type="button"
              data-testid={`part-${o}`}
              aria-pressed={pressed}
              onClick={() => toggle(o)}
              className={`t45-chip${pressed ? ' on' : ''}`}
            >
              {chipLabel(o)}
            </button>
          )
        })}
      </div>
      {field.error && <p className="t45-err mt-2">{field.error}</p>}
    </div>
  )
}

export function PolicyCard({ policy }: { policy: ClaimantPolicy | null | undefined }) {
  if (!policy?.verified) {
    return (
      <div className="rounded-xl bg-[#FDF3DC] p-4 text-sm text-[#5C3D00]">
        We could not find policy {policy?.policy_no} in our records yet. You can still report the loss; an adjuster will confirm your cover.
      </div>
    )
  }
  return (
    <div className="rounded-[10px] border border-[var(--line)] bg-white p-4 text-[var(--text)]">
      <p className="text-lg font-bold">
        {policy.vehicle}{' '}
        <span className="ml-1 rounded-md border border-[var(--line)] bg-[var(--wash)] px-1.5 text-sm font-extrabold tracking-wider text-[var(--brand-deep)]">{policy.plate}</span>
      </p>
      <p className="mt-1 text-sm">Policy {policy.policy_no}, {policy.holder}</p>
      <p className="text-sm">VIN {policy.vin}</p>
      <ul className="mt-2 space-y-0.5 text-sm">
        {(Object.entries(policy.coverages ?? {}) as [string, { deductible?: number; limits?: string }][]).map(([name, c]) => (
          <li key={name}>
            {name[0].toUpperCase() + name.slice(1)}
            {c.deductible !== undefined ? `: $${c.deductible} deductible` : c.limits ? `: ${c.limits}` : ''}
          </li>
        ))}
      </ul>
    </div>
  )
}

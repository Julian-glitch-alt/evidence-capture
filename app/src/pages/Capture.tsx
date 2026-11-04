import { useEffect, useRef, useState, type CSSProperties, type MutableRefObject, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import {
  Car,
  CarFront,
  CircleHelp,
  CloudHail,
  Flame,
  KeyRound,
  Lock,
  PanelTop,
  PawPrint,
  SprayCan,
  TrafficCone,
  Wrench,
  type LucideIcon,
} from 'lucide-react'
import { api, isSessionDead, type Answer, type Answers, type CaptureSession, type ClassifySuggestion, type Field, type Item, type Layout, type PhotoCheck, type SessionState, type Slot, type SlotSkips } from '../api'
import { isLikelyDesktop, useCamera } from '../camera'
import Qr from '../components/Qr'
import CameraScreen from './capture/CameraScreen'
import { FieldInput, PartsField, PolicyCard, SlotTile } from './capture/Fields'
import SignaturePad from './capture/SignaturePad'

const SAVE_DELAY_MS = 500
const DEFAULT_BRAND = { name: 'Demo Mutual Insurance', color: '#1673e6' }

const TYPE_ICONS: Record<string, LucideIcon> = {
  collision_vehicle: Car,
  hit_and_run: CarFront,
  single_vehicle: TrafficCone,
  animal: PawPrint,
  vandalism: SprayCan,
  glass: PanelTop,
  weather: CloudHail,
  parts_theft: Wrench,
  fire: Flame,
  vehicle_theft: KeyRound,
  other_damage: Wrench,
  something_else: CircleHelp,
}

const sessionKey = (token: string) => `capture-session:${token}`

function readStoredSession(token: string): CaptureSession | null {
  try {
    const raw = sessionStorage.getItem(sessionKey(token))
    if (!raw) return null
    const stored = JSON.parse(raw) as CaptureSession
    if (!stored.session_id || !stored.code || !stored.expires_at) return null
    if (Date.parse(stored.expires_at) <= Date.now()) {
      sessionStorage.removeItem(sessionKey(token))
      return null
    }
    return stored
  } catch {
    // private mode: no stored session, the Intro stays
    return null
  }
}

function writeStoredSession(token: string, s: CaptureSession) {
  try {
    sessionStorage.setItem(sessionKey(token), JSON.stringify(s))
  } catch {
    // private mode: the flow still works, it just won't survive a reload
  }
}

function dropStoredSession(token: string) {
  try {
    sessionStorage.removeItem(sessionKey(token))
  } catch {
    // private mode: nothing stored
  }
}

/** A darker shade of the brand for accents (the band gradient end in the mockup). */
function darken(hex: string, f = 0.65): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex.trim())
  if (!m) return '#0b4fa8'
  const n = parseInt(m[1], 16)
  const c = (v: number) => Math.round(v * f).toString(16).padStart(2, '0')
  return `#${c((n >> 16) & 255)}${c((n >> 8) & 255)}${c(n & 255)}`
}

type Screen =
  | { kind: 'picker' }
  | { kind: 'q'; key: string; title: string; fields: Field[] }
  | { kind: 'photos' }
  | { kind: 'check' }
  | { kind: 'send' }

/** One topic per screen: fields sharing a `screen` key (in section order) form one
 *  screen; a field without a key is its own screen. The question screens are
 *  sorted by `layout.screen_order` from the server (a key not in the list, and a
 *  field without a key, go after the listed ones in layout order; the sort is
 *  stable), then Photos when the type has camera slots, then Check, then Sign
 *  and send. N counts them all; it changes when an answer shows or hides a screen. */
function buildScreens(layout: Layout): Screen[] {
  if (!layout.loss_type) return [{ kind: 'picker' }]
  const groups: { key: string; title: string; fields: Field[] }[] = []
  for (const sec of layout.sections) {
    if (sec.id === 'declaration' || sec.id === 'policy') continue
    for (const f of sec.fields) {
      const key = f.screen ?? `field:${f.id}`
      let g = groups.find((x) => x.key === key)
      if (!g) {
        g = { key, title: layout.screen_titles[key] ?? sec.title, fields: [] }
        groups.push(g)
      }
      g.fields.push(f)
    }
  }
  const order = layout.screen_order ?? []
  const rank = (key: string) => {
    const i = order.indexOf(key)
    return i >= 0 ? i : order.length
  }
  const out: Screen[] = groups
    .filter((g) => g.fields.length > 0)
    .sort((a, b) => rank(a.key) - rank(b.key))
    .map((g) => ({ kind: 'q', ...g }))
  const cameraSlots = layout.sections
    .filter((s) => s.id !== 'declaration')
    .flatMap((s) => s.slots)
    .filter((x) => x.kind === 'photo' || x.kind === 'video')
  if (cameraSlots.length > 0) out.push({ kind: 'photos' })
  out.push({ kind: 'check' }, { kind: 'send' })
  return out
}

function screenTitle(s: Screen): string {
  if (s.kind === 'picker') return 'What happened'
  if (s.kind === 'q') return s.title
  if (s.kind === 'photos') return 'Photos'
  if (s.kind === 'check') return 'Check'
  return 'Sign and send'
}

/** The T45 shell on every claimant screen: white header with the brand logo square,
 *  the insurer's name and the claim number; a thin brand progress bar; Step n of N;
 *  a full-width primary button with Back beside it; the trust line with a lock. */
function ClaimShell({
  brand,
  claimNo,
  index,
  total,
  title,
  canBack,
  onBack,
  primary,
  showCount = true,
  progress,
  children,
}: {
  brand: { name: string; color: string }
  claimNo: string
  index: number
  total: number
  title: string
  canBack: boolean
  onBack: () => void
  primary?: { label: string; testid: 'next' | 'send'; onClick: () => void; disabled: boolean }
  /** The picker before a type is chosen has no count: the count depends on the type. */
  showCount?: boolean
  /** Explicit bar fill (done / total); used when showCount is false but progress still applies. */
  progress?: { done: number; total: number }
  children: ReactNode
}) {
  const pct = progress
    ? progress.total > 0
      ? (progress.done / progress.total) * 100
      : 0
    : !showCount
      ? 0
      : total > 0
        ? ((index + 1) / total) * 100
        : 0
  const style = { '--brand': brand.color, '--brand-deep': darken(brand.color) } as CSSProperties
  return (
    <div className="t45" style={style}>
      <div className="t45-col">
        <header className="t45-head">
          <div className="t45-head-row">
            <span className="t45-ins">
              <span className="t45-logo" data-testid="brand-logo">
                {(brand.name.trim().charAt(0) || 'D').toUpperCase()}
              </span>
              {brand.name}
            </span>
            <span className="t45-claim">{claimNo}</span>
          </div>
          <div className="t45-bar">
            <i style={{ width: `${pct}%` }} />
          </div>
          <div className="t45-step" data-testid="step-label">
            {showCount ? (
              <>
                Step {index + 1} of {total}: {title}
              </>
            ) : (
              title
            )}
          </div>
        </header>
        <main className="t45-body">{children}</main>
        <footer className="t45-foot">
          {(canBack || primary) && (
            <div className="t45-btns">
              {canBack && (
                <button data-testid="back" aria-label="Back" onClick={onBack} className="t45-back">
                  ‹
                </button>
              )}
              {primary && (
                <button data-testid={primary.testid} onClick={primary.onClick} disabled={primary.disabled} className="t45-btn">
                  {primary.label}
                </button>
              )}
            </div>
          )}
          <div className="t45-trust">
            <Lock />
            Secure link from {brand.name}. Photos are sealed when they arrive.
          </div>
        </footer>
      </div>
    </div>
  )
}

function Intro({
  brandName,
  claimId,
  token,
  onStart,
  notice,
  policy,
  requested = [],
}: {
  brandName: string
  claimId: string
  token: string
  onStart: (s: CaptureSession) => void
  notice?: string
  policy: SessionState['layout']['policy']
  /** Titles of the shots asked for again after a Send back; empty in round 1. */
  requested?: string[]
}) {
  const [agreed, setAgreed] = useState(false)
  const [desktop, setDesktop] = useState(isLikelyDesktop())
  const [error, setError] = useState<string>()
  const [starting, setStarting] = useState(false)

  const start = async () => {
    setStarting(true)
    setError(undefined)
    try {
      onStart(await api.startSession(token))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not start')
    } finally {
      setStarting(false)
    }
  }

  if (desktop)
    return (
      <div className="t45" style={{ '--brand': DEFAULT_BRAND.color, '--brand-deep': darken(DEFAULT_BRAND.color) } as CSSProperties}>
        <div className="t45-col items-center justify-center gap-5 p-6 text-center">
          <p className="text-sm font-semibold">{brandName}</p>
          <h1 className="t45-q">Continue on your phone</h1>
          <p className="t45-help">Photos must be taken with a phone camera in this page. Scan this code with your phone to continue.</p>
          <Qr value={window.location.href} size={220} />
          <button className="text-sm underline" onClick={() => setDesktop(false)}>
            Continue on this device anyway
          </button>
        </div>
      </div>
    )

  return (
    <div className="t45" style={{ '--brand': DEFAULT_BRAND.color, '--brand-deep': darken(DEFAULT_BRAND.color) } as CSSProperties}>
      <div className="t45-col">
        <main className="t45-body">
          <p className="text-sm font-semibold">{brandName}</p>
          <h1 className="t45-q">Send photos and details for claim {claimId}</h1>
          {requested.length > 0 ? (
            <>
              <p className="text-[15px]">Your adjuster looked at your claim and needs these again:</p>
              <ul className="list-disc space-y-1 pl-5 text-[15px] font-semibold">
                {requested.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
              <p className="text-[15px]">It takes a minute. Your answers and other photos stay as you sent them.</p>
            </>
          ) : (
            <>
              <div className="mb-1">
                <PolicyCard policy={policy} />
              </div>
              <ul className="list-disc space-y-2 pl-5 text-[15px]">
                <li>About 5 minutes. Your answers save as you go, and this link works until the claim is sent.</li>
                <li>Photos and video are taken with the camera in this page; there is no option to pick existing photos.</li>
                <li>Everything you send is sealed when it reaches us, so nobody can change it later.</li>
              </ul>
            </>
          )}
          <label className="flex gap-3 text-sm">
            <input type="checkbox" className="mt-0.5 h-5 w-5" checked={agreed} onChange={(e) => setAgreed(e.target.checked)} />
            I agree to share my answers, and any photos and video I take here, with {brandName} for this claim.
          </label>
          <p className="t45-note">
            Photos are read by AI that keeps no copy, and files are deleted 90 days after the decision.{' '}
            <a href="/privacy" target="_blank" rel="noopener">
              Privacy notice
            </a>
          </p>
          {notice && <p className="rounded-lg bg-[#FDF3DC] px-3 py-2 text-sm">{notice}</p>}
          {error && <p className="text-sm text-red-700">{error}</p>}
        </main>
        <footer className="t45-foot">
          <div className="t45-btns">
            <button onClick={start} disabled={!agreed || starting} className="t45-btn">
              {starting ? 'Starting…' : 'Start'}
            </button>
          </div>
          <div className="t45-trust">
            <Lock />
            Secure link from {brandName}. Photos are sealed when they arrive.
          </div>
        </footer>
      </div>
    </div>
  )
}

function Picker({
  brand,
  claimNo,
  layout,
  token,
  savedDescription,
  onPick,
}: {
  brand: { name: string; color: string }
  claimNo: string
  layout: Layout
  token: string
  /** The draft's description, if the claimant already wrote one. */
  savedDescription: string | undefined
  /** Select a card; a description is also saved when one is given. */
  onPick: (id: string, description?: string) => void
}) {
  const [text, setText] = useState('')
  const [sent, setSent] = useState('')
  const [suggestion, setSuggestion] = useState<ClassifySuggestion | null>(null)
  const [failed, setFailed] = useState(false)
  const [running, setRunning] = useState(false)
  const ready = text.trim().length >= 8

  const find = async () => {
    if (!ready || running) return
    setRunning(true)
    setSuggestion(null)
    setFailed(false)
    try {
      const r = await api.classify(token, text)
      setSent(text)
      if (r.loss_type) setSuggestion(r)
      else setFailed(true)
    } catch {
      setSent(text)
      setFailed(true)
    } finally {
      setRunning(false)
    }
  }

  const groups: { name: string; types: Layout['loss_types'] }[] = []
  for (const t of layout.loss_types) {
    const g = groups.find((x) => x.name === t.group)
    if (g) g.types.push(t)
    else groups.push({ name: t.group, types: [t] })
  }
  return (
    <ClaimShell brand={brand} claimNo={claimNo} index={0} total={1} title="What happened" canBack={false} onBack={() => {}} showCount={false}>
      <h1 className="t45-q">What happened to your car?</h1>
      <p className="t45-help">Tell us in a few words, or pick the closest answer below.</p>
      <div className="t45-field mt-3">
        <label htmlFor="own-words">Tell us what happened</label>
        <p className="t45-help mb-2">A few words are enough. You can pick from the list below instead.</p>
        <textarea
          id="own-words"
          aria-label="Tell us what happened"
          placeholder="For example: a rock hit the hood on the highway"
          maxLength={1000}
          rows={3}
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
      </div>
      <div className="t45-btns mt-3">
        <button onClick={() => void find()} disabled={!ready || running} className="t45-btn">
          {running ? 'Reading what you wrote…' : 'Find the right type'}
        </button>
      </div>
      {suggestion?.loss_type && (
        <div data-testid="type-suggestion" className="mt-3 rounded-[10px] border border-[var(--line)] bg-white px-3 py-3">
          <p className="font-semibold">It looks like: {suggestion.label}</p>
          <p className="mt-1 text-sm text-[var(--mute)]">“{sent}”</p>
          <div className="t45-btns mt-3">
            <button
              data-testid="type-accept"
              className="t45-btn"
              onClick={() =>
                onPick(
                  suggestion.loss_type as string,
                  savedDescription === undefined || savedDescription.trim() === '' ? sent : undefined,
                )
              }
            >
              Yes, that&apos;s right
            </button>
            <button data-testid="type-reject" onClick={() => setSuggestion(null)} className="px-1 text-sm underline">
              No, let me choose
            </button>
          </div>
        </div>
      )}
      {failed && !suggestion && (
        <p className="t45-help mt-2">We couldn&apos;t tell. Please pick the closest answer below.</p>
      )}
      <h2 className="t45-group">Or pick the closest answer</h2>
      {groups.map((g) => (
        <div key={g.name}>
          <h2 className="t45-group">{g.name}</h2>
          <div className="mt-2 flex flex-col gap-2">
            {g.types.map((t) => {
              const Icon = TYPE_ICONS[t.id] ?? CircleHelp
              return (
                <button key={t.id} data-testid={`type-${t.id}`} onClick={() => onPick(t.id)} className="t45-opt">
                  <span className="t45-ico">
                    <Icon />
                  </span>
                  <span>
                    {t.label}
                    <small>{t.description}</small>
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      ))}
      <p className="t45-note mt-2">Your answers save as you go. You can close this page and come back with the same link.</p>
    </ClaimShell>
  )
}

export default function Capture() {
  const { token = '' } = useParams()
  const camera = useCamera()
  const [state, setState] = useState<SessionState>()
  const [answers, setAnswers] = useState<Answers>({})
  const [loadError, setLoadError] = useState<string>()
  // A reload must not force the claimant through Start again: reuse the stored
  // session while it has not expired. A superseded session fails later with a
  // 403, which sends the claimant back to the Intro with a short message.
  const [session, setSession] = useState<CaptureSession | undefined>(() => readStoredSession(token) ?? undefined)
  const [screenIndex, setScreenIndex] = useState(0)
  const [cameraSlot, setCameraSlot] = useState<Slot>()
  const [qualityFails, setQualityFails] = useState<Record<string, number>>({})
  const [error, setError] = useState<string>()
  const [sending, setSending] = useState(false)
  const [done, setDone] = useState(false)
  const [introNotice, setIntroNotice] = useState<string>()
  const saveSeq = useRef(0)
  const dirty = useRef(false)

  useEffect(() => {
    api
      .session(token)
      .then((s) => {
        setState(s)
        setAnswers(s.answers)
        if (s.status !== 'opened' && s.status !== 'capturing') setDone(true)
      })
      .catch((e) => setLoadError(e.message))
  }, [token])

  const handleStart = (s: CaptureSession) => {
    writeStoredSession(token, s)
    setIntroNotice(undefined)
    setSession(s)
  }

  const handleSessionInvalid = () => {
    dropStoredSession(token)
    setCameraSlot(undefined)
    setSession(undefined)
    setIntroNotice('Your session was restarted. Tap Start to continue.')
  }

  // Save the draft shortly after the claimant stops typing; the server answers with the new layout.
  useEffect(() => {
    if (!dirty.current) return
    const seq = ++saveSeq.current
    const timer = setTimeout(() => {
      api
        .saveDraft(token, answers)
        .then((s) => {
          if (seq !== saveSeq.current) return // a newer save is on its way
          setState(s)
        })
        .catch((e) => setError(e.message))
    }, SAVE_DELAY_MS)
    return () => clearTimeout(timer)
  }, [answers, token])

  const brand = state?.brand ?? DEFAULT_BRAND

  if (loadError)
    return (
      <div className="t45" style={{ '--brand': DEFAULT_BRAND.color, '--brand-deep': darken(DEFAULT_BRAND.color) } as CSSProperties}>
        <Centered title="Link unavailable" text={loadError} />
      </div>
    )
  if (!state)
    return (
      <div className="t45" style={{ '--brand': DEFAULT_BRAND.color, '--brand-deep': darken(DEFAULT_BRAND.color) } as CSSProperties}>
        <Centered title="Loading…" />
      </div>
    )
  const displayClaim = state.insurer_claim_number ?? state.claim_id
  if (done)
    return (
      <div className="t45" style={{ '--brand': brand.color, '--brand-deep': darken(brand.color) } as CSSProperties}>
        <div data-testid="done" className="flex min-h-full flex-col items-center gap-3 p-6 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-[var(--brand)] text-2xl text-white">✓</div>
          <h1 className="text-xl font-bold">Claim sent</h1>
          <div className="w-full max-w-sm rounded-[10px] border border-[var(--line)] bg-white px-4 py-3 text-left">
            <p className="text-xs font-semibold text-[var(--mute)]">Claim number</p>
            <p className="text-lg font-bold">{displayClaim}</p>
          </div>
          <p className="max-w-sm text-[var(--mute)]">
            {(state.requested_slots ?? []).length > 0
              ? `${brand.name} has your new photos for claim ${displayClaim}, sealed as they arrived. Your adjuster will take it from here. You can close this page.`
              : (state.layout.loss_type ?? answers.loss_type) === 'something_else'
                ? `${brand.name} has received claim ${displayClaim}. An adjuster will call you${answers.callback_phone ? ` at ${answers.callback_phone}` : ''} to take it forward. You can close this page.`
                : `${brand.name} has received claim ${displayClaim} with your photos, video and answers, sealed as they arrived. You can close this page.`}
          </p>
        </div>
      </div>
    )
  const { layout } = state
  const slotById = new Map<string, Slot>(layout.sections.flatMap((s) => s.slots).map((s) => [s.id, s]))
  const requested = state.requested_slots ?? []
  if (!session)
    return (
      <Intro
        brandName={brand.name}
        claimId={displayClaim}
        token={token}
        onStart={handleStart}
        notice={introNotice}
        policy={layout.policy}
        requested={requested.map((id) => slotById.get(id)?.title ?? id)}
      />
    )

  return (
    <CaptureFlow
      key={token}
      token={token}
      state={state}
      setState={setState}
      answers={answers}
      setAnswers={setAnswers}
      session={session}
      displayClaim={displayClaim}
      brand={brand}
      camera={camera}
      cameraSlot={cameraSlot}
      setCameraSlot={setCameraSlot}
      qualityFails={qualityFails}
      setQualityFails={setQualityFails}
      error={error}
      setError={setError}
      sending={sending}
      setSending={setSending}
      screenIndex={screenIndex}
      setScreenIndex={setScreenIndex}
      setDone={setDone}
      handleSessionInvalid={handleSessionInvalid}
      dirty={dirty}
    />
  )
}

type Camera = ReturnType<typeof useCamera>

function CaptureFlow({
  token,
  state,
  setState,
  answers,
  setAnswers,
  session,
  displayClaim,
  brand,
  camera,
  cameraSlot,
  setCameraSlot,
  qualityFails,
  setQualityFails,
  error,
  setError,
  sending,
  setSending,
  screenIndex,
  setScreenIndex,
  setDone,
  handleSessionInvalid,
  dirty,
}: {
  token: string
  state: SessionState
  setState: (s: SessionState) => void
  answers: Answers
  setAnswers: (a: Answers) => void
  session: CaptureSession
  displayClaim: string
  brand: { name: string; color: string }
  camera: Camera
  cameraSlot: Slot | undefined
  setCameraSlot: (s: Slot | undefined) => void
  qualityFails: Record<string, number>
  setQualityFails: (m: Record<string, number>) => void
  error: string | undefined
  setError: (e: string | undefined) => void
  sending: boolean
  setSending: (b: boolean) => void
  screenIndex: number
  setScreenIndex: (n: number) => void
  setDone: (b: boolean) => void
  handleSessionInvalid: () => void
  dirty: MutableRefObject<boolean>
}) {
  const { layout, items } = state
  const slotById = new Map<string, Slot>(layout.sections.flatMap((s) => s.slots).map((s) => [s.id, s]))
  const photoChecks = state.photo_checks ?? []
  const screens = buildScreens(layout)
  const index = Math.min(screenIndex, screens.length - 1)

  // While a photo check is still running on the Photos or Check screen, refresh
  // until none is pending. A result never blocks; it only informs.
  useEffect(() => {
    if (!layout.loss_type) return
    const current = screens[Math.min(screenIndex, screens.length - 1)]
    if (current.kind !== 'photos' && current.kind !== 'check') return
    if (!photoChecks.some((c) => c.result === 'pending')) return
    const timer = setInterval(() => {
      api.session(token).then(setState).catch(() => {})
    }, 2000)
    return () => clearInterval(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, state, screenIndex])

  const setAnswer = (id: string, v: Answer) => {
    dirty.current = true
    setAnswers({ ...answers, [id]: v })
  }
  /** T59: a choice is a suggestion. Choosing another option clears the companion
   *  text in the same update (two spreads of one snapshot would drop the first). */
  const setField = (f: Field, v: Answer) => {
    dirty.current = true
    if (f.other != null && !selectsOther(v, f.other)) {
      setAnswers({ ...answers, [f.id]: v, [`${f.id}_other`]: '' })
    } else {
      setAnswers({ ...answers, [f.id]: v })
    }
  }
  const setOtherText = (f: Field, v: string) => {
    dirty.current = true
    setAnswers({ ...answers, [`${f.id}_other`]: v })
  }
  /** The live `<id>_other` text; undefined falls back to the layout's `other_text`. */
  const otherValue = (f: Field): string | undefined => {
    const t = answers[`${f.id}_other`]
    return typeof t === 'string' ? t : undefined
  }
  const itemsFor = (slotId: string) => items.filter((i) => i.step_id === slotId && !i.superseded)
  const onUploaded = (item: Item) => {
    // Refresh layout so slot counts and "complete" come from the server.
    api.session(token).then(setState)
    setState({ ...state, items: [...items.filter((i) => i.step_id !== item.step_id || cameraSlot?.multiple), item] })
  }
  const skips = (answers.slot_skips ?? {}) as SlotSkips
  const setSkip = (slotId: string, skip: { reason: string; note?: string } | null) => {
    const next = { ...skips }
    if (skip) next[slotId] = skip
    else delete next[slotId]
    setAnswer('slot_skips', next)
  }
  const onRemove = async (itemId: string) => {
    try {
      await api.removeItem(token, session.session_id, itemId)
      setState(await api.session(token))
    } catch (e) {
      if (isSessionDead(e)) handleSessionInvalid()
      else setError(e instanceof Error ? e.message : 'Could not remove the photo. Try again.')
    }
  }
  const retakePhoto = async (check: PhotoCheck) => {
    const slot = slotById.get(check.slot_id)
    if (!slot) return
    try {
      // A multi-photo slot drops the flagged shot first; a single shot is replaced by the new one.
      if (slot.multiple) await api.removeItem(token, session.session_id, check.item_id)
      setState(await api.session(token))
      setCameraSlot(slot)
    } catch (e) {
      if (isSessionDead(e)) handleSessionInvalid()
      else setError(e instanceof Error ? e.message : 'Could not open the camera for a retake. Try again.')
    }
  }
  const photoSections = layout.sections.filter((s) => s.id !== 'declaration' && s.slots.some((x) => x.kind !== 'signature'))
  const cameraScreen = (step?: { n: number; total: number }) =>
    cameraSlot && (
      <CameraScreen
        token={token}
        sessionId={session.session_id}
        sessionCode={session.code}
        slot={cameraSlot}
        camera={camera}
        onUploaded={onUploaded}
        onClose={() => setCameraSlot(undefined)}
        onSessionInvalid={handleSessionInvalid}
        thresholds={layout.quality}
        qualityFails={qualityFails[cameraSlot.id] ?? 0}
        onQualityFail={() => {
          const id = cameraSlot.id
          setQualityFails({ ...qualityFails, [id]: (qualityFails[id] ?? 0) + 1 })
        }}
        step={step}
        brand={brand}
      />
    )
  const sendNow = async () => {
    setSending(true)
    setError(undefined)
    try {
      await api.submit(token, answers)
      dropStoredSession(token)
      camera.stop()
      setDone(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not send. Check your signal and try again.')
    } finally {
      setSending(false)
    }
  }

  // Round 2 after a Send back (track 3): only the requested shots can be taken; the
  // answers and every other shot were sealed in round 1 and are shown read-only.
  const requested = state.requested_slots ?? []
  if (requested.length > 0) {
    const left = new Set(state.retakes_left ?? requested)
    const asked = requested.map((id) => slotById.get(id)).filter((x): x is Slot => !!x)
    const retaken = asked.filter((x) => !left.has(x.id)).length
    const nextId = asked.find((x) => left.has(x.id))?.id
    // Checks of the new shots only: a round-1 shot still waiting for its retake says nothing here.
    const askedChecks = photoChecks.filter((c) => requested.includes(c.slot_id) && !left.has(c.slot_id))
    const waiting = askedChecks.some((c) => c.result === 'pending' || c.result === 'retake')
    const kept = layout.sections
      .flatMap((sec) => sec.slots)
      .filter((x) => x.kind !== 'signature' && !requested.includes(x.id) && itemsFor(x.id).length > 0)
    const answered = layout.sections.filter((sec) => sec.id !== 'declaration' && sec.fields.some((f) => !isEmpty(answers[f.id])))
    const plural = asked.length === 1 ? 'photo' : 'photos'
    return (
      <>
        <ClaimShell
          brand={brand}
          claimNo={displayClaim}
          index={0}
          total={1}
          title="More photos"
          showCount={false}
          progress={{ done: retaken, total: asked.length }}
          canBack={false}
          onBack={() => {}}
          primary={{
            label: sending ? 'Sending…' : `Send the new ${plural}`,
            testid: 'send',
            onClick: sendNow,
            disabled: left.size > 0 || waiting || sending,
          }}
        >
          <div role="status" className="mb-5 rounded-2xl border-2 border-[#E8C46A] bg-[#FDF3DC] p-4 text-[#5C3D00]">
            <p className="text-xl font-extrabold leading-tight text-[var(--text)]">
              Your insurer asked for {asked.length} more {plural}
            </p>
            <p className="mt-1.5 text-[15px]">
              Take {asked.length === 1 ? 'it' : 'them'} below, then send. Your answers and other photos stay as you sent them.
            </p>
          </div>
          <Progress done={retaken} total={asked.length} label={`${retaken} of ${asked.length} new ${plural} taken`} />
          <div className="mb-5 space-y-3">
            {asked.map((x, i) => (
              <SlotTile
                key={x.id}
                slot={x}
                items={itemsFor(x.id)}
                fileUrl={(id) => api.claimantFileUrl(token, id)}
                onOpen={() => setCameraSlot(x)}
                onRemove={onRemove}
                number={i + 1}
                next={x.id === nextId}
                done={!left.has(x.id)}
                mode="retake"
              />
            ))}
          </div>
          <PhotoCheckList checks={askedChecks} titleOf={(id) => slotById.get(id)?.title ?? id} onRetake={retakePhoto} />
          {kept.length > 0 && (
            <Summary title="Kept as you sent them">
              <div className="flex flex-wrap gap-2 py-1">
                {kept.flatMap((x) =>
                  itemsFor(x.id).map((item) =>
                    item.kind === 'video' ? (
                      <div key={item.id} className="flex h-16 w-16 items-center justify-center rounded-md bg-[#2a3036] text-xs text-white">Video</div>
                    ) : (
                      <img key={item.id} src={api.claimantFileUrl(token, item.id)} alt={x.title} className="h-16 w-16 rounded-md object-cover" />
                    ),
                  ),
                )}
              </div>
            </Summary>
          )}
          {answered.length > 0 && <h2 className="mb-2 mt-6 text-lg font-bold">Your answers, as you sent them</h2>}
          {answered.map((sec) => (
            <Summary key={sec.id} title={sec.title}>
              {sec.fields
                .filter((f) => !isEmpty(answers[f.id]))
                .map((f) => (
                  <div key={f.id} className="grid grid-cols-[40%_1fr] gap-2 py-1 text-sm">
                    <dt className="text-[var(--mute)]">{f.label}</dt>
                    <dd>{show(answers[f.id])}</dd>
                  </div>
                ))}
            </Summary>
          ))}
          {error && <p className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
          {(left.size > 0 || waiting) && (
            <p className="mt-2 text-center text-sm text-[var(--mute)]">
              {left.size > 0
                ? `Still needed: ${asked.filter((x) => left.has(x.id)).map((x) => x.title).join(', ')}`
                : askedChecks.some((c) => c.result === 'pending')
                  ? 'Checking your photos…'
                  : 'Retake the photo marked above to continue.'}
            </p>
          )}
        </ClaimShell>
        {cameraScreen(cameraSlot && asked.some((x) => x.id === cameraSlot.id)
          ? { n: asked.findIndex((x) => x.id === cameraSlot.id) + 1, total: asked.length }
          : undefined)}
      </>
    )
  }

  if (!layout.loss_type) {
    return (
      <Picker
        brand={brand}
        claimNo={displayClaim}
        layout={layout}
        token={token}
        savedDescription={typeof answers.description === 'string' ? answers.description : undefined}
        onPick={async (id, description) => {
          const next = { ...answers, loss_type: id, ...(description !== undefined ? { description } : {}) }
          const s = await api.saveDraft(token, next)
          setState(s)
          setAnswers(s.answers)
          setScreenIndex(0)
          window.scrollTo(0, 0)
        }}
      />
    )
  }

  const screen = screens[index]
  const goScreen = (n: number) => {
    setScreenIndex(Math.max(0, Math.min(n, screens.length - 1)))
    window.scrollTo(0, 0)
  }
  const screenForField = (fid: string) => screens.findIndex((s) => s.kind === 'q' && s.fields.some((f) => f.id === fid))
  const photosIdx = screens.findIndex((s) => s.kind === 'photos')

  const formSections = layout.sections.filter((s) => s.fields.length > 0 && s.id !== 'declaration')
  const photoBlocked = photoChecks.some((c) => c.result === 'pending' || c.result === 'retake')
  const declaration = layout.sections.find((s) => s.id === 'declaration')!

  // T59b: a required field the policy filled in counts as answered until the claimant types.
  const fieldGaps = formSections.flatMap((sec) =>
    sec.fields.filter((f) => (f.required && isEmpty(answers[f.id]) && !f.prefilled) || (f.error && answers[f.id] === f.value)).map((f) => f.label),
  )
  const photoGaps = photoSections.flatMap((sec) => sec.slots.filter((x) => x.required && x.count === 0 && !x.skipped).map((x) => x.title))
  // The shots asked for, in order: numbered on the tiles and on the camera ("Photo 3 of 7").
  const shots = photoSections.flatMap((sec) => sec.slots).filter((x) => x.required && x.kind !== 'signature')
  const shotsDone = shots.filter((x) => x.count > 0 || x.skipped).length
  const nextShot = shots.find((x) => x.count === 0 && !x.skipped)?.id

  const screenReady = (s: Screen): boolean => {
    if (s.kind === 'picker') return false
    if (s.kind === 'photos') return photoGaps.length === 0
    if (s.kind === 'check') return fieldGaps.length === 0 && photoGaps.length === 0
    if (s.kind === 'send') return layout.complete
    return s.fields.every((f) => {
      // T59b: a prefilled required field counts as answered (the server agrees at save time).
      if (f.required && isEmpty(answers[f.id]) && !f.prefilled) return false
      if (f.error && answers[f.id] === f.value) return false
      // T59: the escape option with no words yet counts as unanswered, like a
      // required field left empty (the server's `missing` agrees at save time).
      if (f.other != null && selectsOther(answers[f.id], f.other)) {
        const t = answers[`${f.id}_other`]
        if (typeof t !== 'string' || t.trim() === '') return false
      }
      return true
    })
  }

  const back = async () => {
    if (index > 0) return goScreen(index - 1)
    // Back on the first screen: choose what happened again.
    const rest = Object.fromEntries(Object.entries(answers).filter(([k]) => k !== 'loss_type'))
    setAnswers(rest)
    setState(await api.saveDraft(token, rest))
  }

  const slotTile = (s: Slot) => (
    <SlotTile
      key={s.id}
      slot={s}
      items={itemsFor(s.id)}
      fileUrl={(id) => api.claimantFileUrl(token, id)}
      onOpen={() => setCameraSlot(s)}
      onRemove={onRemove}
      skipReasons={layout.skip_reasons}
      onSkip={(skip) => setSkip(s.id, skip)}
      number={shots.some((x) => x.id === s.id) ? shots.findIndex((x) => x.id === s.id) + 1 : undefined}
      next={s.id === nextShot}
    />
  )

  const body = (
    <>
      {screen.kind === 'q' && (
        <>
          <h1 className="t45-q">{screen.title}</h1>
          {screen.fields.map((f) =>
            f.widget === 'car_parts' && f.type === 'multiselect' ? (
              <div key={f.id}>
                {f.hint && <p className="t45-help mb-2">{f.hint}</p>}
                <PartsField
                  field={f}
                  value={Array.isArray(answers[f.id]) ? (answers[f.id] as string[]) : []}
                  onChange={(v) => setAnswer(f.id, v)}
                />
              </div>
            ) : (
              <FieldInput
                key={f.id}
                field={f}
                value={answers[f.id]}
                otherValue={otherValue(f)}
                onChange={(v) => setField(f, v)}
                onOtherChange={(v) => setOtherText(f, v)}
              />
            ),
          )}
        </>
      )}

      {screen.kind === 'photos' && (
        <>
          <h1 className="t45-q">Photos</h1>
          <p className="t45-help">Take each one with this phone in this page. If you can't take one, say why and continue.</p>
          <div className="rounded-[10px] border border-[var(--line)] bg-white p-3 text-sm text-[var(--text)]">
            <span className="mr-2 inline-block rounded-md bg-[var(--brand)] px-2.5 py-1 text-xs font-bold text-white">
              Session {session.code}
            </span>
            appears on every photo
          </div>
          <Progress done={shotsDone} total={shots.length} label={`${shotsDone} of ${shots.length} done`} />
          {photoSections.map((sec) => (
            <div key={sec.id}>
              <h2 className="t45-group">{sec.title}</h2>
              <div className="mt-2 space-y-3">{sec.slots.filter((x) => x.kind !== 'signature').map(slotTile)}</div>
            </div>
          ))}
          {photoGaps.length > 0 && <p className="text-sm text-[var(--mute)]">Still needed: {photoGaps.join(', ')}</p>}
        </>
      )}

      {screen.kind === 'check' && (
        <>
          <h1 className="t45-q">Check</h1>
          <p className="t45-help">What we checked so far, and your claim as it will be sent.</p>
          <PhotoCheckList checks={photoChecks} titleOf={(id) => slotById.get(id)?.title ?? id} onRetake={retakePhoto} />
          <ul className="mb-6 space-y-2">
            {checks(layout, answers, photoSections, brand.name).map((c) => (
              <li key={c.text} className={`flex gap-3 rounded-lg px-3 py-2 text-sm ${c.ok ? 'bg-[#E9F3EE]' : 'border border-[#f3d27a] bg-[#fff6dc]'}`}>
                <span className={`font-bold ${c.ok ? 'text-[#00694A]' : 'text-[#8A5A00]'}`}>{c.ok ? '✓' : '!'}</span>
                <span>{c.text}</span>
              </li>
            ))}
          </ul>
          {formSections.map((sec) => (
            <Summary
              key={sec.id}
              title={sec.title}
              onEdit={() => {
                const i = sec.fields.length ? screenForField(sec.fields[0].id) : -1
                if (i >= 0) goScreen(i)
              }}
            >
              {sec.fields.every((f) => isEmpty(answers[f.id])) && <p className="py-1 text-sm text-[var(--mute)]">Nothing entered</p>}
              {sec.fields
                .filter((f) => !isEmpty(answers[f.id]))
                .map((f) => (
                  <div key={f.id} className="grid grid-cols-[40%_1fr] gap-2 py-1 text-sm">
                    <dt className="text-[var(--mute)]">{f.label}</dt>
                    <dd>{show(answers[f.id])}</dd>
                  </div>
                ))}
            </Summary>
          ))}
          {photoSections.length > 0 && (
            <Summary title="Photos" onEdit={() => photosIdx >= 0 && goScreen(photosIdx)}>
              <div className="flex flex-wrap gap-2 py-1">
                {photoSections.flatMap((sec) => sec.slots).flatMap((x) =>
                  itemsFor(x.id).map((item) =>
                    item.kind === 'video' ? (
                      <div key={item.id} className="flex h-16 w-16 items-center justify-center rounded-md bg-[#2a3036] text-xs text-white">Video</div>
                    ) : (
                      <img key={item.id} src={api.claimantFileUrl(token, item.id)} alt={x.title} className="h-16 w-16 rounded-md object-cover" />
                    ),
                  ),
                )}
              </div>
            </Summary>
          )}
        </>
      )}

      {screen.kind === 'send' && (
        <>
          <h1 className="t45-q">Sign and send</h1>
          {declaration.notice && (
            <div className="mb-4 border-l-[3px] border-[var(--brand)] bg-[var(--wash)] px-3 py-2 text-sm">
              {declaration.notice}
              <p className="mt-1 text-xs text-[var(--mute)]">State fraud warning (demo wording)</p>
            </div>
          )}
          {declaration.fields.map((f) => (
            <FieldInput
              key={f.id}
              field={f}
              value={answers[f.id]}
              otherValue={otherValue(f)}
              onChange={(v) => setField(f, v)}
              onOtherChange={(v) => setOtherText(f, v)}
            />
          ))}
          {declaration.slots.map((s) => (
            <SignaturePad
              key={s.id}
              token={token}
              sessionId={session.session_id}
              slot={s}
              item={itemsFor(s.id)[0]}
              fileUrl={(id) => api.claimantFileUrl(token, id)}
              onUploaded={onUploaded}
              onSessionInvalid={handleSessionInvalid}
            />
          ))}
          {!layout.complete && <p className="mt-4 text-sm text-[var(--mute)]">Before sending: {layout.missing.join(', ')}</p>}
        </>
      )}

      {error && <p className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
      {screen.kind === 'check' && photoBlocked && (
        <p className="text-sm text-[var(--mute)]">
          {photoChecks.some((c) => c.result === 'pending') ? 'Checking your photos…' : 'Retake the photos marked above to continue.'}
        </p>
      )}
    </>
  )

  const primary =
    screen.kind === 'send'
      ? { label: sending ? 'Sending…' : 'Send my claim', testid: 'send' as const, onClick: sendNow, disabled: !layout.complete || sending }
      : {
          label: 'Continue',
          testid: 'next' as const,
          onClick: () => goScreen(index + 1),
          disabled: !screenReady(screen) || (screen.kind === 'check' && photoBlocked),
        }

  return (
    <>
      <ClaimShell
        brand={brand}
        claimNo={displayClaim}
        index={index}
        total={screens.length}
        title={screenTitle(screen)}
        canBack
        onBack={() => void back()}
        primary={primary}
      >
        {body}
      </ClaimShell>
      {cameraScreen(cameraSlot && shots.some((x) => x.id === cameraSlot.id)
        ? { n: shots.findIndex((x) => x.id === cameraSlot.id) + 1, total: shots.length }
        : undefined)}
    </>
  )
}

function isEmpty(v: Answer | undefined) {
  return v === undefined || v === '' || v === false || (Array.isArray(v) && v.length === 0)
}

/** T59: the escape option is chosen: a `choice` equal to it, a `multiselect` holding it. */
function selectsOther(v: Answer | undefined, other: string): boolean {
  return Array.isArray(v) ? v.includes(other) : v === other
}

function show(v: Answer | undefined) {
  if (Array.isArray(v)) return v.join(', ')
  if (v === true) return 'Yes'
  return typeof v === 'string' ? v : ''
}

/** Plain checks we can already make without any model. Model checks will join this list later. */
function checks(layout: SessionState['layout'], answers: Answers, photoSections: SessionState['layout']['sections'], insurer: string) {
  const out: { ok: boolean; text: string }[] = []
  const policy = layout.policy
  out.push(
    policy?.verified
      ? { ok: true, text: `Policy ${policy.policy_no} found in ${insurer}'s records` }
      : { ok: false, text: `Policy ${policy?.policy_no ?? ''} not found yet. An adjuster will confirm your cover.` },
  )
  const vin = layout.sections.flatMap((s) => s.fields).find((f) => f.id === 'vin')
  if (vin && answers.vin) {
    out.push(
      vin.error
        ? { ok: false, text: `VIN: ${vin.error}` }
        : vin.prefilled
          ? { ok: true, text: 'VIN matches your policy' }
          : vin.note === 'Matches your policy'
            ? { ok: true, text: 'VIN is valid and matches your policy' }
            : vin.note
              ? { ok: false, text: `VIN is valid. ${vin.note}` }
              : { ok: false, text: 'VIN is valid but could not be compared with your policy.' },
    )
  }
  const slots = photoSections.flatMap((s) => s.slots).filter((x) => x.required && x.kind !== 'signature')
  if (slots.length) {
    const taken = slots.filter((x) => x.count > 0).length
    const skipped = slots.filter((x) => x.count === 0 && x.skipped)
    out.push({ ok: taken === slots.length, text: `${taken} of ${slots.length} required photos and video taken in this page` })
    skipped.forEach((x) => out.push({ ok: false, text: `${x.title} not taken: ${x.skipped!.reason}` }))
  }
  return out
}

function Progress({ done, total, label }: { done: number; total: number; label: string }) {
  return (
    <div className="mb-5" aria-live="polite">
      <div className="mb-1.5 flex items-baseline justify-between text-sm">
        <span className="font-semibold">{label}</span>
        {done === total && <span className="font-semibold text-[#00694A]">✓ All done</span>}
      </div>
      <div className="h-2.5 overflow-hidden rounded-full bg-[var(--line)]" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
        <div className="h-full rounded-full bg-[var(--brand)] transition-all" style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
      </div>
    </div>
  )
}

/** What the photo checks said, in plain words. A retake hint never says what did not match. */
function PhotoCheckList({ checks, titleOf, onRetake }: { checks: PhotoCheck[]; titleOf: (slotId: string) => string; onRetake: (c: PhotoCheck) => void }) {
  if (!checks.length) return null
  return (
    <ul className="mb-6 space-y-2">
      {checks.map((c) => {
        const siblings = checks.filter((x) => x.slot_id === c.slot_id)
        const title = `${titleOf(c.slot_id)}${siblings.length > 1 ? ` ${siblings.indexOf(c) + 1}` : ''}`
        if (c.result === 'pass')
          return (
            <li key={c.item_id} className="flex gap-3 rounded-lg bg-[#E9F3EE] px-3 py-2.5 text-[15px]">
              <span className="font-bold text-[#00694A]">✓</span>
              <span>{title} looks good</span>
            </li>
          )
        if (c.result === 'retake')
          return (
            <li key={c.item_id} className="rounded-lg border border-[#f3d27a] bg-[#fff6dc] px-3 py-2.5 text-[15px]">
              <div className="flex gap-3">
                <span className="font-bold text-[#8A5A00]">!</span>
                <span>
                  <b>{title}.</b> {c.hint}
                </span>
              </div>
              <div className="mt-2 flex items-center gap-3 pl-6">
                <button onClick={() => onRetake(c)} className="min-h-11 rounded-[10px] bg-[var(--brand)] px-5 py-2 text-[15px] font-semibold text-white">
                  Retake
                </button>
                <span className="text-sm text-[var(--mute)]">
                  {c.shots_left} more {c.shots_left === 1 ? 'try' : 'tries'}
                </span>
              </div>
            </li>
          )
        if (c.result === 'flagged')
          return (
            <li key={c.item_id} className="flex gap-3 rounded-lg border border-[var(--line)] bg-white px-3 py-2.5 text-[15px]">
              <span className="font-bold text-[var(--mute)]">!</span>
              <span>{title}: we will pass this photo to your adjuster. You can carry on.</span>
            </li>
          )
        if (c.result === 'unchecked')
          return (
            <li key={c.item_id} className="flex gap-3 rounded-lg border border-[var(--line)] bg-white px-3 py-2.5 text-[15px]">
              <span className="font-bold text-[var(--mute)]">✓</span>
              <span>{title} saved</span>
            </li>
          )
        return (
          <li key={c.item_id} className="flex gap-3 rounded-lg border border-[var(--line)] bg-white px-3 py-2.5 text-[15px]">
            <span className="font-bold text-[var(--mute)]">…</span>
            <span>Checking {title}…</span>
          </li>
        )
      })}
    </ul>
  )
}

function Summary({ title, onEdit, children }: { title: string; onEdit?: () => void; children: ReactNode }) {
  return (
    <section className="mb-4 rounded-[10px] border border-[var(--line)] bg-white px-3 py-2">
      <div className="flex items-center justify-between">
        <h2 className="font-bold">{title}</h2>
        {onEdit && (
          <button onClick={onEdit} className="min-h-11 px-1 text-sm font-semibold text-[var(--brand)] underline">
            Edit
          </button>
        )}
      </div>
      <dl>{children}</dl>
    </section>
  )
}

function Centered({ title, text, icon, testid }: { title: string; text?: string; icon?: string; testid?: string }) {
  return (
    <div data-testid={testid} className="flex min-h-full flex-col items-center justify-center gap-3 p-6 text-center">
      {icon && <div className="flex h-14 w-14 items-center justify-center rounded-full bg-[var(--wash)] text-2xl text-[var(--brand)]">{icon}</div>}
      <h1 className="text-xl font-bold">{title}</h1>
      {text && <p className="max-w-sm text-[var(--mute)]">{text}</p>}
    </div>
  )
}

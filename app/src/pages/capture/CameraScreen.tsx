import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { api, isSessionDead, type Item, type Slot, type UploadProgress } from '../../api'
import type { useCamera } from '../../camera'
import { judge, MAX_QUALITY_FAILS, QUALITY_HINTS, QUALITY_VERSION, type Quality, type QualityProblem, type Thresholds } from '../../quality'
import FrameGuide from './FrameGuide'
import { guideLine } from './frameGuides'

type Camera = ReturnType<typeof useCamera>
type Shot = { blob: Blob; url: string; meta: Record<string, unknown>; quality?: Quality; problem?: QualityProblem | null; fails?: number; frameBefore?: Blob | null; frameAfter?: Blob | null }

const NONCE_REFRESH_MS = 150_000 // nonces live 3 minutes on the server

/** A darker shade of the brand, matching ClaimShell in Capture.tsx. */
function darken(hex: string, f = 0.65): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex.trim())
  if (!m) return '#0b4fa8'
  const n = parseInt(m[1], 16)
  const c = (v: number) => Math.round(v * f).toString(16).padStart(2, '0')
  return `#${c((n >> 16) & 255)}${c((n >> 8) & 255)}${c(n & 255)}`
}

interface Props {
  token: string
  sessionId: string
  sessionCode: string
  slot: Slot
  camera: Camera
  onUploaded: (item: Item) => void
  onClose: () => void
  onSessionInvalid?: (message: string) => void
  thresholds?: Thresholds
  qualityFails: number
  onQualityFail: () => void
  /** Where this shot sits among the shots asked for, e.g. Photo 3 of 7. */
  step?: { n: number; total: number }
  brand: { name: string; color: string }
}

/** Full-screen live camera for one slot. Asks the server for a nonce when it opens. */
export default function CameraScreen({ token, sessionId, sessionCode, slot, camera, onUploaded, onClose, onSessionInvalid, thresholds, qualityFails, onQualityFail, step, brand }: Props) {
  const [shot, setShot] = useState<Shot>()
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const [error, setError] = useState<string>()
  const [recording, setRecording] = useState<{ stop: () => void } | null>(null)
  const [seconds, setSeconds] = useState(0)
  const nonceRef = useRef<{ nonce: string; at: number } | null>(null)
  const attemptRef = useRef(0)
  const abortRef = useRef<AbortController | null>(null)

  const pct = progress?.total ? Math.min(100, Math.round((progress.loaded / progress.total) * 100)) : null
  // At 100% the file is out; the server is checking and sealing it.
  const sendingLabel = pct === 100 ? 'Saving…' : slot.kind === 'video' ? 'Sending video…' : 'Sending photo…'

  const freshNonce = async () => {
    if (!nonceRef.current || Date.now() - nonceRef.current.at > NONCE_REFRESH_MS) {
      const { nonce } = await api.nonce(token, sessionId, slot.id)
      nonceRef.current = { nonce, at: Date.now() }
    }
    return nonceRef.current.nonce
  }

  const deadMessage = (e: unknown) => (e instanceof Error ? e.message : 'This capture session is not active.')

  useEffect(() => {
    camera.start()
    freshNonce().catch((e) => {
      if (isSessionDead(e)) onSessionInvalid?.(deadMessage(e))
      else setError(e instanceof Error ? e.message : 'Could not get the camera ready. Close it and try again.')
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slot.id])

  useEffect(() => () => shot && URL.revokeObjectURL(shot.url), [shot])

  const capture = async () => {
    setError(undefined)
    try {
      if (slot.kind === 'photo') {
        const { blob, meta, quality, frameBefore, frameAfter } = await camera.takePhoto()
        const problem = thresholds && quality ? judge(quality, thresholds) : null
        if (problem) onQualityFail()
        setShot({ blob, meta, url: URL.createObjectURL(blob), quality: quality ?? undefined, problem, fails: qualityFails + (problem ? 1 : 0), frameBefore, frameAfter })
      } else if (recording) {
        recording.stop()
      } else {
        setSeconds(0)
        const rec = camera.recordVideo(slot.max_seconds ?? 15, setSeconds)
        setRecording(rec)
        const { blob, meta } = await rec.done
        setRecording(null)
        setShot({ blob, meta, url: URL.createObjectURL(blob) })
      }
    } catch (e) {
      setRecording(null)
      setError(e instanceof Error && e.message === 'Camera is not ready yet' ? 'The camera is still starting. Wait a second and try again.' : 'Could not take the shot. Try again.')
    }
  }

  /** Drop the in-flight upload, if any. Later results are ignored. */
  const dropAttempt = () => {
    attemptRef.current++
    abortRef.current?.abort()
    abortRef.current = null
  }

  const retake = () => {
    dropAttempt()
    setShot(undefined)
    setBusy(false)
    setProgress(null)
    setError(undefined)
  }

  const use = async () => {
    if (!shot) return
    const my = ++attemptRef.current
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setBusy(true)
    setError(undefined)
    setProgress({ loaded: 0, total: shot.blob.size })
    try {
      const nonce = await freshNonce()
      if (my !== attemptRef.current) return
      const item = await api.uploadItem(token, sessionId, slot.id, nonce, shot.blob, {
        ...shot.meta,
        session_code_shown: sessionCode,
        ...(shot.quality
          ? { quality: { sharpness: shot.quality.sharpness, brightness: shot.quality.brightness, problem: shot.problem ?? null, fails: shot.fails ?? 0, version: QUALITY_VERSION } }
          : {}),
      }, {
        signal: ctrl.signal,
        frameBefore: shot.frameBefore,
        frameAfter: shot.frameAfter,
        onProgress: (p) => {
          if (my === attemptRef.current) setProgress(p)
        },
      })
      if (my !== attemptRef.current) return
      nonceRef.current = null // single use
      onUploaded(item)
      onClose()
    } catch (e) {
      if (my !== attemptRef.current) return
      // Same nonce if still fresh, else a fresh one: Try again calls use, which
      // calls freshNonce, which only refetches past NONCE_REFRESH_MS.
      if (isSessionDead(e)) {
        onSessionInvalid?.(deadMessage(e))
        return
      }
      setError(e instanceof Error ? `Not sent: ${e.message}` : 'Not sent. Check your signal and tap Try again.')
      setProgress(null)
    } finally {
      if (my === attemptRef.current) setBusy(false)
    }
  }

  const qualityProblem = shot?.problem ?? null
  const qualityFailsNow = shot?.fails ?? qualityFails
  const blockedByQuality = qualityProblem != null && qualityFailsNow < MAX_QUALITY_FAILS
  const style = { '--brand': brand.color, '--brand-deep': darken(brand.color) } as CSSProperties

  return (
    <div className="t45 fixed inset-0 z-20 flex flex-col" style={{ ...style, background: '#fff' }}>
      <div className="flex items-center justify-between gap-2 border-b border-[var(--line)] bg-white px-4 py-3">
        <p className="truncate text-[15px] font-bold text-[var(--text)]">{slot.title}</p>
        <p className="flex-none text-[13px] text-[var(--mute)]">
          {step ? `Photo ${step.n} of ${step.total}` : slot.kind === 'video' ? 'Video' : 'Photo'}
        </p>
      </div>
      <main className="relative min-h-0 flex-1 overflow-hidden bg-[#0E1311]">
        {shot ? (
          slot.kind === 'video' ? (
            <video src={shot.url} controls playsInline className="h-full w-full object-contain" />
          ) : (
            <img src={shot.url} alt="Captured" className="h-full w-full object-contain" />
          )
        ) : (
          <>
            <video ref={camera.attach} autoPlay muted playsInline className="h-full w-full object-cover" />
            {camera.status === 'ready' && <FrameGuide slotId={slot.id} />}
            {camera.status !== 'ready' && (
              <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-sm text-slate-300">
                {camera.error ?? 'Opening camera…'}
              </div>
            )}
          </>
        )}
        <div className="absolute left-3 top-3 rounded-md bg-[var(--brand)] px-2.5 py-1 text-xs font-bold text-white">
          Session <span data-testid="session-code">{sessionCode}</span>
        </div>
        {recording && (
          <div className="absolute left-4 top-16 rounded-full bg-red-600 px-3 py-1 text-sm font-medium text-white">
            ● {Math.floor(seconds)}s / {slot.max_seconds}s
          </div>
        )}
        {!shot && (
          <div className="absolute inset-x-3 bottom-3 rounded-[10px] bg-white p-3 text-[13.5px] leading-snug text-[var(--text)]">
            <span className="font-bold">Tip: </span>
            {guideLine(slot.id, slot.instructions)}
          </div>
        )}
      </main>

      <footer className="space-y-3 bg-white px-4 pb-8 pt-4">
        {error && <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}
        {shot && qualityProblem && (
          <div className="rounded-lg bg-[#FDF3DC] px-3 py-2 text-sm text-[#5C3D00]">
            <p>{QUALITY_HINTS[qualityProblem]}</p>
            {qualityFailsNow >= MAX_QUALITY_FAILS && <p className="mt-1">We will pass this photo to your adjuster.</p>}
          </div>
        )}
        {busy && (
          <div aria-live="polite">
            <p className="mb-1 text-center text-sm font-medium text-[var(--text)]">
              {sendingLabel}
              {pct != null && ` ${pct}%`}
            </p>
            <div className="h-2 overflow-hidden rounded bg-[var(--line)]">
              <div
                className={`h-2 rounded bg-[var(--brand)] ${pct == null ? 'w-full animate-pulse' : ''}`}
                style={pct != null ? { width: `${pct}%` } : undefined}
              />
            </div>
          </div>
        )}
        {shot ? (
          blockedByQuality ? (
            <button onClick={retake} className="w-full rounded-[10px] border border-[var(--line)] bg-white py-4 font-semibold text-[var(--text)]">
              Retake
            </button>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <button onClick={retake} className="rounded-[10px] border border-[var(--line)] bg-white py-4 font-semibold text-[var(--text)]">
                Retake
              </button>
              <button onClick={use} disabled={busy} className="rounded-[10px] bg-[var(--brand)] py-4 font-semibold text-white disabled:opacity-60">
                {busy ? 'Sending…' : error ? 'Try again' : slot.kind === 'video' ? 'Use video' : 'Use photo'}
              </button>
            </div>
          )
        ) : (
          <div className="flex items-center justify-between px-4">
            <button data-testid="camera-cancel" aria-label="Cancel" onClick={onClose} disabled={!!recording} className="flex h-[50px] w-[50px] items-center justify-center rounded-[10px] border border-[var(--line)] bg-white text-[22px] leading-none text-[var(--text)] disabled:opacity-50">
              ‹
            </button>
            <button
              data-testid="shutter"
              onClick={capture}
              disabled={camera.status !== 'ready'}
              aria-label={slot.kind === 'photo' ? 'Take photo' : recording ? 'Stop recording' : 'Start recording'}
              className="flex h-[76px] w-[76px] items-center justify-center rounded-full border-[5px] border-[var(--brand)] bg-white disabled:opacity-40"
            >
              <span
                className={
                  slot.kind === 'photo'
                    ? 'h-14 w-14 rounded-full bg-[var(--brand)]'
                    : recording
                      ? 'h-7 w-7 rounded-sm bg-red-600'
                      : 'h-14 w-14 rounded-full bg-red-600'
                }
              />
            </button>
            <span className="w-[50px]" />
          </div>
        )}
        {shot && busy && (
          <button
            onClick={() => {
              dropAttempt()
              onClose()
            }}
            className="w-full py-1 text-center text-sm text-[var(--mute)] underline"
          >
            Cancel
          </button>
        )}
      </footer>
    </div>
  )
}

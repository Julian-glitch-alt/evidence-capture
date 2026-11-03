import { useEffect, useRef, useState } from 'react'
import { api, isSessionDead, type Item, type Slot } from '../../api'

interface Props {
  token: string
  sessionId: string
  slot: Slot
  item: Item | undefined
  fileUrl: (itemId: string) => string
  onUploaded: (item: Item) => void
  onSessionInvalid?: (message: string) => void
}

/** Finger or stylus signature on a canvas, sent as a PNG through the same nonce path as photos. */
export default function SignaturePad({ token, sessionId, slot, item, fileUrl, onUploaded, onSessionInvalid }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const drawing = useRef(false)
  const [inked, setInked] = useState(false)
  const [drawingNow, setDrawingNow] = useState(false)
  const [editing, setEditing] = useState(!item)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !editing) return
    const ratio = window.devicePixelRatio || 1
    canvas.width = canvas.clientWidth * ratio
    canvas.height = canvas.clientHeight * ratio
    const ctx = canvas.getContext('2d')!
    ctx.scale(ratio, ratio)
    ctx.lineWidth = 2.5
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    ctx.strokeStyle = '#142236'
  }, [editing])

  const point = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    return [e.clientX - r.left, e.clientY - r.top] as const
  }
  const down = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.currentTarget.setPointerCapture(e.pointerId)
    drawing.current = true
    setDrawingNow(true)
    const ctx = e.currentTarget.getContext('2d')!
    ctx.beginPath()
    ctx.moveTo(...point(e))
  }
  const stop = () => {
    drawing.current = false
    setDrawingNow(false)
  }
  const move = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawing.current) return
    const ctx = e.currentTarget.getContext('2d')!
    ctx.lineTo(...point(e))
    ctx.stroke()
    setInked(true)
  }
  const clear = () => {
    const canvas = canvasRef.current!
    canvas.getContext('2d')!.clearRect(0, 0, canvas.width, canvas.height)
    setInked(false)
  }

  const save = async () => {
    setBusy(true)
    setError(undefined)
    try {
      const blob = await new Promise<Blob>((resolve, reject) =>
        canvasRef.current!.toBlob((b) => (b ? resolve(b) : reject(new Error('Could not read the signature'))), 'image/png'),
      )
      const { nonce } = await api.nonce(token, sessionId, slot.id)
      const saved = await api.uploadItem(token, sessionId, slot.id, nonce, blob, {
        captured_at: new Date().toISOString(),
        input: 'signature_pad',
      })
      onUploaded(saved)
      setEditing(false)
    } catch (e) {
      if (isSessionDead(e)) {
        onSessionInvalid?.(e instanceof Error ? e.message : 'This capture session is not active.')
        return
      }
      setError(e instanceof Error ? e.message : 'Could not save the signature')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      data-testid="signature-pad"
      className="rounded-[10px] border bg-white p-3"
      style={{ borderColor: drawingNow ? 'var(--brand)' : 'var(--line)' }}
    >
      <div className="mb-2 flex items-start justify-between">
        <p className="font-bold">{slot.title}</p>
        {item && !editing && <span className="rounded-full bg-[#00694A] px-2 py-0.5 text-xs font-bold text-white">✓ Received</span>}
      </div>
      {item && !editing ? (
        <>
          <img src={fileUrl(item.id)} alt="Your signature" className="h-28 w-full rounded-lg bg-white object-contain" />
          <button onClick={() => setEditing(true)} className="mt-3 w-full rounded-[10px] border border-[var(--line)] bg-white py-2 text-sm font-semibold text-[var(--text)]">
            Sign again
          </button>
        </>
      ) : (
        <>
          <p className="mb-2 text-xs text-[var(--mute)]">{slot.instructions}</p>
          <canvas
            ref={canvasRef}
            aria-label="Signature box"
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={stop}
            onPointerLeave={stop}
            className="h-36 w-full touch-none rounded-lg border border-[var(--line)] bg-white"
          />
          {error && <p className="mt-2 text-sm text-red-700">{error}</p>}
          <div className="mt-3 grid grid-cols-2 gap-2">
            <button onClick={clear} disabled={!inked || busy} className="rounded-[10px] border border-[var(--line)] bg-white py-2 text-sm font-semibold text-[var(--text)] disabled:opacity-50">
              Clear
            </button>
            <button onClick={save} disabled={!inked || busy} className="rounded-[10px] border border-[var(--line)] bg-white py-2 text-sm font-semibold text-[var(--text)] disabled:opacity-50">
              {busy ? 'Saving…' : 'Use this signature'}
            </button>
          </div>
        </>
      )}
    </div>
  )
}

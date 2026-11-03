import { useCallback, useEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type WheelEvent as ReactWheelEvent } from 'react'
import { api, formatTime, type Damage, type Item } from '../api'

function outlineRan(o: Damage['outline']): boolean {
  return !!o && o.status !== 'not_run'
}

function photoHasOutline(damages: Damage[]): boolean {
  return damages.some((d) => d.box && outlineRan(d.outline))
}

function outlineNote(d: Damage): string {
  const o = d.outline
  if (!o || o.sam_agrees === 'not_run') return 'outline not available'
  if (o.sam_agrees === 'no') return 'the outline model found nothing in this box'
  const a = o.area
  if (!a || a.photo_pct == null) return 'outlined'
  let text = `outline ${a.photo_pct}% of photo`
  if (a.panel_pct != null) text += ` · ${a.panel_pct}% of ${a.panel ?? 'panel'}`
  return a.basis === 'mask' ? text : `${text} (box estimate)`
}

export interface ViewerPhoto {
  item: Item
  label: string
}

export default function PhotoViewer({
  photos,
  damagesByItem,
  initialId,
  onClose,
}: {
  photos: ViewerPhoto[]
  damagesByItem: Record<string, Damage[]>
  initialId: string
  onClose: () => void
}) {
  const [currentId, setCurrentId] = useState(initialId)
  const [compareId, setCompareId] = useState<string | null>(null)
  const [showBoxes, setShowBoxes] = useState(true)
  const [showOutlines, setShowOutlines] = useState(true)
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const prevFocus = useRef<Element | null>(null)
  const drag = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null)

  const current = photos.find((p) => p.item.id === currentId) ?? photos[0]
  const compare = compareId ? (photos.find((p) => p.item.id === compareId) ?? null) : null
  const showOutlineUI =
    photoHasOutline((current ? damagesByItem[current.item.id] : undefined) ?? []) ||
    (compare ? photoHasOutline(damagesByItem[compare.item.id] ?? []) : false)

  useEffect(() => {
    prevFocus.current = document.activeElement
    closeRef.current?.focus()
    return () => {
      if (prevFocus.current instanceof HTMLElement) prevFocus.current.focus()
    }
  }, [])

  const clampZoom = useCallback((z: number) => Math.min(4, Math.max(1, Math.round(z * 10) / 10)), [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
        return
      }
      if (e.key === 'Tab' && dialogRef.current) {
        const els = dialogRef.current.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        const list = Array.from(els).filter((el) => !el.hasAttribute('disabled'))
        if (list.length === 0) return
        const first = list[0]
        const last = list[list.length - 1]
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault()
          last.focus()
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [onClose])

  const pick = (id: string) => {
    setCurrentId(id)
    setZoom(1)
    setPan({ x: 0, y: 0 })
  }

  const onWheel = (e: ReactWheelEvent) => {
    e.preventDefault()
    const next = clampZoom(zoom + (e.deltaY < 0 ? 0.25 : -0.25))
    setZoom(next)
    if (next === 1) setPan({ x: 0, y: 0 })
  }

  const startDrag = (e: ReactMouseEvent) => {
    if (zoom === 1) return
    drag.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y }
  }
  const moveDrag = (e: ReactMouseEvent) => {
    const d = drag.current
    if (!d) return
    setPan({ x: d.panX + (e.clientX - d.x), y: d.panY + (e.clientY - d.y) })
  }
  const endDrag = () => {
    drag.current = null
  }

  const renderPhoto = (photo: ViewerPhoto, testId: string) => {
    const damages = damagesByItem[photo.item.id] ?? []
    if (photo.item.removed_at) {
      return (
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="grid min-h-[280px] flex-1 place-items-center rounded-sm bg-[#1f262d] text-slate-300">
            Removed
          </div>
          <p className="text-[12px] text-[var(--ink-2)]">
            {photo.label} · removed {formatTime(photo.item.removed_at ?? null)}
          </p>
        </div>
      )
    }
    return (
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div
          className="flex min-h-[280px] flex-1 cursor-grab items-center justify-center overflow-hidden rounded-sm bg-[#1f262d] active:cursor-grabbing"
          onWheel={onWheel}
          onMouseDown={startDrag}
          onMouseMove={moveDrag}
          onMouseUp={endDrag}
          onMouseLeave={endDrag}
        >
          {/* The frame is exactly the displayed photo, so boxes and outlines (percent of the photo)
              land on it whatever its shape, and zoom and pan with it. */}
          <div
            className="relative max-w-full"
            style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, transformOrigin: 'center' }}
          >
            <img
              src={api.fileUrl(photo.item.id)}
              alt={photo.label}
              data-testid={testId}
              className="block max-h-[60vh] max-w-full"
              draggable={false}
            />
            {showOutlines &&
              damages.flatMap((d, i) =>
                (d.outline?.regions ?? []).map((r, j) =>
                  r.overlay ? (
                    <img
                      key={`outline-${d.key}-${i}-${j}`}
                      src={`data:image/png;base64,${r.overlay}`}
                      alt=""
                      data-testid="sam-outline"
                      className="pointer-events-none absolute"
                      style={{
                        left: `${r.box[0] / 10}%`,
                        top: `${r.box[1] / 10}%`,
                        width: `${Math.max(0, r.box[2] - r.box[0]) / 10}%`,
                        height: `${Math.max(0, r.box[3] - r.box[1]) / 10}%`,
                        imageRendering: 'pixelated',
                      }}
                    />
                  ) : (
                    <span
                      key={`outline-${d.key}-${i}-${j}`}
                      data-testid="sam-outline"
                      className="pointer-events-none absolute border-2 border-[#00b8ff]"
                      style={{
                        left: `${r.box[0] / 10}%`,
                        top: `${r.box[1] / 10}%`,
                        width: `${Math.max(0, r.box[2] - r.box[0]) / 10}%`,
                        height: `${Math.max(0, r.box[3] - r.box[1]) / 10}%`,
                      }}
                    />
                  ),
                ),
              )}
            {showBoxes &&
              damages.map((d, i) =>
                d.box ? (
                  <span
                    key={`${d.key}-${i}`}
                    className="desk-box"
                    style={{
                      left: `${d.box[0] / 10}%`,
                      top: `${d.box[1] / 10}%`,
                      width: `${Math.max(0, d.box[2] - d.box[0]) / 10}%`,
                      height: `${Math.max(0, d.box[3] - d.box[1]) / 10}%`,
                    }}
                  >
                    <i>{typeof d.number === 'number' && d.number > 0 ? d.number : i + 1}</i>
                  </span>
                ) : null,
              )}
          </div>
        </div>
        <p className="text-[12px] text-[var(--ink-2)]">
          {photo.label} · received {formatTime(photo.item.received_at)}
        </p>
        {damages.some((d) => d.box && outlineRan(d.outline)) && (
          <ul className="text-[11.5px] text-[var(--ink-3)]" aria-label="Outline notes">
            {damages.map((d, i) =>
              d.box && outlineRan(d.outline) ? (
                <li key={`note-${d.key}-${i}`}>
                  #{typeof d.number === 'number' && d.number > 0 ? d.number : i + 1}: {outlineNote(d)}
                </li>
              ) : null,
            )}
          </ul>
        )}
        <p className="truncate font-mono text-[11px] text-[var(--ink-3)]" title={photo.item.sha256}>
          sha256 {photo.item.sha256}
        </p>
      </div>
    )
  }

  if (!current) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Photo ${current.label}`}
        className="flex max-h-[90vh] w-full max-w-5xl flex-col gap-3 overflow-auto rounded-md bg-white p-4"
      >
        <div className="flex flex-wrap items-center gap-3">
          <strong className="text-[14px]">{current.label}</strong>
          <label className="flex items-center gap-2 text-[12px]">
            <input type="checkbox" checked={showBoxes} onChange={(e) => setShowBoxes(e.target.checked)} />
            Boxes on/off
          </label>
          {showOutlineUI && (
            <label className="flex items-center gap-2 text-[12px]">
              <input type="checkbox" checked={showOutlines} onChange={(e) => setShowOutlines(e.target.checked)} />
              Outlines
            </label>
          )}
          <span className="text-[11.5px] text-[var(--ink-3)]">
            {showOutlineUI ? 'outline: SAM 3.1 · box: Muse Spark' : 'box: Muse Spark'}
          </span>
          <div className="flex items-center gap-1" role="group" aria-label="Zoom">
            <button type="button" className="desk-act" onClick={() => setZoom((z) => clampZoom(z - 0.5))} aria-label="Zoom out">
              −
            </button>
            <span className="desk-mono" aria-live="polite">
              {zoom.toFixed(1)}×
            </span>
            <button type="button" className="desk-act" onClick={() => setZoom((z) => clampZoom(z + 0.5))} aria-label="Zoom in">
              +
            </button>
            <button
              type="button"
              className="desk-act"
              onClick={() => {
                setZoom(1)
                setPan({ x: 0, y: 0 })
              }}
            >
              Reset
            </button>
          </div>
          <label className="flex items-center gap-2 text-[12px]">
            Compare
            <select
              value={compareId ?? ''}
              onChange={(e) => setCompareId(e.target.value || null)}
              className="rounded-sm border border-[var(--rule)] bg-white px-2 py-1"
            >
              <option value="">Off</option>
              {photos
                .filter((p) => p.item.id !== currentId)
                .map((p) => (
                  <option key={p.item.id} value={p.item.id}>
                    {p.label}
                  </option>
                ))}
            </select>
          </label>
          <button ref={closeRef} type="button" className="desk-act primary ml-auto" onClick={onClose}>
            Close
          </button>
        </div>
        <div className="flex flex-col gap-2">
          <label className="text-[12px] text-[var(--ink-2)]">
            Photo
            <select
              value={currentId}
              onChange={(e) => pick(e.target.value)}
              className="ml-2 rounded-sm border border-[var(--rule)] bg-white px-2 py-1"
            >
              {photos.map((p) => (
                <option key={p.item.id} value={p.item.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <div className={`flex gap-4 ${compare ? 'flex-col md:flex-row' : 'flex-col'}`}>
            {renderPhoto(current, 'viewer-main')}
            {compare && renderPhoto(compare, 'viewer-compare')}
          </div>
          <p className="text-[11.5px] text-[var(--ink-3)]">
            Scroll to zoom (1×–4×), drag to pan. Press Esc or Close to return.
          </p>
        </div>
      </div>
    </div>
  )
}

import { useId, type KeyboardEvent } from 'react'

/** T45: the approved top-view sedan (tasks/t45-assets/car-top.svg).
 *
 *  Claimant use (`<CarDiagram selected={...} />`, no `onArea`): an illustration
 *  only — the drawing never takes pointer events; chips elsewhere drive
 *  `selected` (class `on`).
 *
 *  Console use (`states`, `markers`, `active`, `onArea`): the same drawing with
 *  one interactive shape per area. Each shape is a focusable `role="button"`
 *  with `aria-pressed` for the active area; `states` maps area →
 *  `{seen, declared}`; `markers` maps area → text drawn at the area (e.g. the
 *  damage numbers). Class names carry the state: `seen`, `declared`, `missing`
 *  (declared but not seen). */

export interface CarAreaState {
  seen: number
  declared: boolean
}

const HL: { area: string; d?: string; x?: number; y?: number; w?: number; h?: number; cx: number; cy: number }[] = [
  { area: 'Front bumper', x: -30, y: -10, w: 280, h: 50, cx: 110, cy: 15 },
  { area: 'Rear bumper', x: -30, y: 424, w: 280, h: 40, cx: 110, cy: 444 },
  { area: 'Left side', x: -30, y: 160, w: 96, h: 162, cx: 18, cy: 241 },
  { area: 'Right side', x: 154, y: 160, w: 96, h: 162, cx: 202, cy: 241 },
  { area: 'Front left', d: 'M-30 40 L52 40 L58 152 L36 166 L-30 166 Z', cx: 14, cy: 105 },
  { area: 'Front right', d: 'M 250 40 L 168 40 L 162 152 L 184 166 L 250 166 Z', cx: 206, cy: 105 },
  { area: 'Rear left', d: 'M-30 318 L66 318 L56 364 L54 424 L-30 430 Z', cx: 18, cy: 375 },
  { area: 'Rear right', d: 'M 250 318 L 154 318 L 164 364 L 166 424 L 250 430 Z', cx: 202, cy: 375 },
  { area: 'Hood', d: 'M52 40 L168 40 L162 150 C130 142 90 142 58 150 Z', cx: 110, cy: 95 },
  { area: 'Windshield', d: 'M58 152 C90 142 130 142 162 152 L152 200 C126 194 94 194 68 200 Z', cx: 110, cy: 176 },
  { area: 'Roof', d: 'M68 202 C94 196 126 196 152 202 L154 318 C128 324 92 324 66 318 Z', cx: 110, cy: 260 },
  { area: 'Rear window', d: 'M66 320 C92 326 128 326 154 320 L164 362 C130 372 90 372 56 362 Z', cx: 110, cy: 343 },
  { area: 'Trunk or tailgate', d: 'M56 364 C90 374 130 374 164 364 L166 424 C130 432 90 432 54 424 Z', cx: 110, cy: 398 },
]

/** The four tyre rects (x, y, w, h) and their centres for the Wheels marker. */
const TYRES = [
  { x: 15, y: 76, w: 13, h: 54 },
  { x: 192, y: 76, w: 13, h: 54 },
  { x: 15, y: 336, w: 13, h: 56 },
  { x: 192, y: 336, w: 13, h: 56 },
]
const TYRE_CENTRES = [
  { x: 21.5, y: 103 },
  { x: 198.5, y: 103 },
  { x: 21.5, y: 364 },
  { x: 198.5, y: 364 },
]

const norm = (s: string) => s.trim().toLowerCase()

export default function CarDiagram({
  selected = [],
  states,
  markers,
  active = null,
  onArea,
}: {
  /** Claimant: the chosen areas light up (class `on`). */
  selected?: string[]
  /** Console: area → {seen, declared} (keys may be exact or lower-cased). */
  states?: Record<string, CarAreaState>
  /** Console: area → text drawn at the area, e.g. the damage numbers. */
  markers?: Record<string, string>
  /** Console: the area filtering the table. */
  active?: string | null
  /** Console: when present the shapes are interactive; without it the drawing
   *  takes no pointer events, as on the claimant side. */
  onArea?: (area: string) => void
}) {
  const uid = useId().replace(/[^a-zA-Z0-9]/g, '')
  const interactive = onArea != null

  if (!interactive) {
    const on = (area: string) => (selected.includes(area) ? ' on' : '')
    return (
      <div className="car">
        <svg
          className="carsvg"
          viewBox="-34 -26 288 500"
          role="img"
          aria-label="Sedan seen from above, front at the top; the parts you choose turn yellow"
        >
          <defs>
            <clipPath id={`clip${uid}`}>
              <path d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z" />
            </clipPath>
            <pattern id={`grid${uid}`} width="16" height="16" patternUnits="userSpaceOnUse">
              <path d="M16 0 L0 0 0 16" fill="none" stroke="#c7dcf7" strokeWidth="1" />
            </pattern>
          </defs>
          <rect x="-34" y="-26" width="288" height="500" rx="16" fill="#eaf3ff" />
          <rect x="-34" y="-26" width="288" height="500" rx="16" fill={`url(#grid${uid})`} />
          <g className={`tyre${on('Wheels or tires')}`} data-area="Wheels or tires">
            <rect x="15" y="76" width="13" height="54" rx="6" />
            <rect x="192" y="76" width="13" height="54" rx="6" />
            <rect x="15" y="336" width="13" height="56" rx="6" />
            <rect x="192" y="336" width="13" height="56" rx="6" />
          </g>
          <path className={`mirror${on('Left side')}`} data-area="Left side" d="M30 144 C18 140 10 148 12 157 C18 162 26 160 31 156 Z" />
          <path className={`mirror${on('Right side')}`} data-area="Right side" d="M 190 144 C 202 140 210 148 208 157 C 202 162 194 160 189 156 Z" />
          <path
            d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z"
            fill="#ffffff"
          />
          <g clipPath={`url(#clip${uid})`}>
            <g className="hls">
              {HL.map((h) =>
                h.d ? (
                  <path key={h.area} className={`hl${on(h.area)}`} data-area={h.area} d={h.d} />
                ) : (
                  <rect key={h.area} className={`hl${on(h.area)}`} data-area={h.area} x={h.x} y={h.y} width={h.w} height={h.h} />
                ),
              )}
            </g>
          </g>
          <g className="ink">
            <path d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z" />
            <path d="M58 152 C90 142 130 142 162 152 L152 200 C126 194 94 194 68 200 Z" />
            <path d="M68 202 C94 196 126 196 152 202 L154 318 C128 324 92 324 66 318 Z" />
            <path d="M66 320 C92 326 128 326 154 320 L164 362 C130 372 90 372 56 362 Z" />
            <path d="M56 364 C90 374 130 374 164 364 L166 424 C130 432 90 432 54 424 Z" />
            <path d="M52 42 C54 80 56 120 58 150" />
            <path d="M 168 42 C 166 80 164 120 162 150" />
            <path d="M84 50 C88 90 90 120 88 146" className="thin" />
            <path d="M 136 50 C 132 90 130 120 132 146" className="thin" />
            <path d="M40 38 C70 30 150 30 180 38" className="thin" />
            <path d="M44 424 C70 432 150 432 176 424" className="thin" />
            <path d="M36 172 C40 162 50 158 60 158 L64 248 L34 248 Z" />
            <path d="M34 254 L64 254 L65 314 C55 314 43 310 36 300 Z" />
            <path d="M 184 172 C 180 162 170 158 160 158 L 156 248 L 186 248 Z" />
            <path d="M 186 254 L 156 254 L 155 314 C 165 314 177 310 184 300 Z" />
            <path d="M31 172 C33 220 33 270 31 330" className="thin" />
            <path d="M 189 172 C 187 220 187 270 189 330" className="thin" />
            <path d="M24 251 L34 251" />
            <path d="M186 251 L196 251" />
            <path d="M38 40 C42 28 56 22 74 22 L76 32 C62 34 50 38 44 48 Z" />
            <path d="M 182 40 C 178 28 164 22 146 22 L 144 32 C 158 34 170 38 176 48 Z" />
            <path d="M38 424 C44 436 58 442 74 442 L75 434 C61 433 50 429 45 420 Z" className="tail" />
            <path d="M 182 424 C 176 436 162 442 146 442 L 145 434 C 159 433 170 429 175 420 Z" className="tail" />
            <path d="M86 160 L98 150 M92 172 L110 156 M128 172 L140 162" className="thin" />
            <path d="M84 336 L96 328 M120 344 L138 332" className="thin" />
            <path d="M92 20 C104 18 116 18 128 20" className="thin" />
          </g>
          <text x="110" y="-10" className="cl" textAnchor="middle">FRONT</text>
          <text x="-20" y="236" className="cl" textAnchor="middle" transform="rotate(-90 -20 236)">Driver&apos;s side (left)</text>
          <text x="240" y="236" className="cl" textAnchor="middle" transform="rotate(90 240 236)">Passenger side (right)</text>
        </svg>
      </div>
    )
  }

  // ---- Console: the same drawing, one interactive shape per area ----
  const statesByKey: Record<string, CarAreaState> = {}
  for (const [k, v] of Object.entries(states ?? {})) statesByKey[norm(k)] = v
  const markersByKey: Record<string, string> = {}
  for (const [k, v] of Object.entries(markers ?? {})) markersByKey[norm(k)] = v
  const activeKey = active != null ? norm(active) : null
  const push = onArea as (area: string) => void
  const stateFor = (area: string): CarAreaState => statesByKey[norm(area)] ?? { seen: 0, declared: false }

  const shape = (area: string) => {
    const st = stateFor(area)
    const isActive = activeKey === norm(area)
    const seen = st.seen > 0
    const missing = st.declared && !seen
    // Console colors: sun fill for seen; outlines from the console tokens.
    const fill = area === 'Wheels or tires' && !seen && !missing ? '#15212c' : seen ? '#ffc23d' : '#ffffff'
    let stroke = 'var(--ink-3)'
    let dash: string | undefined
    if (seen && st.declared) stroke = 'var(--good)'
    else if (seen && !st.declared) {
      stroke = 'var(--warn)'
      dash = '4 2'
    } else if (missing) stroke = 'var(--bad)'
    if (isActive) {
      stroke = 'var(--ink)'
      dash = undefined
    }
    const activate = () => push(area)
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        push(area)
      }
    }
    return {
      role: 'button' as const,
      tabIndex: 0,
      'aria-pressed': isActive,
      'aria-label': `${area}: ${st.seen} seen, ${st.declared ? 'declared' : 'not declared'}. Activate to filter.`,
      'data-area': area,
      className: [seen ? 'seen' : '', st.declared ? 'declared' : '', missing ? 'missing' : ''].filter(Boolean).join(' '),
      fill,
      stroke,
      strokeWidth: isActive ? 2.5 : 1.5,
      strokeDasharray: dash,
      style: { cursor: 'pointer' } as const,
      onClick: activate,
      onKeyDown,
    }
  }

  const inkStroke = 'var(--ink)'
  return (
    <div className="car" style={{ display: 'flex', justifyContent: 'center' }}>
      <svg
        viewBox="-34 -26 288 500"
        width={195}
        height={338}
        role="group"
        aria-label="Damage map, sedan seen from above, front at the top"
      >
        <defs>
          <clipPath id={`clip${uid}`}>
            <path d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z" />
          </clipPath>
          <pattern id={`grid${uid}`} width="16" height="16" patternUnits="userSpaceOnUse">
            <path d="M16 0 L0 0 0 16" fill="none" stroke="#c7dcf7" strokeWidth="1" />
          </pattern>
        </defs>
        <rect x="-34" y="-26" width="288" height="500" rx="16" fill="#eaf3ff" />
        <rect x="-34" y="-26" width="288" height="500" rx="16" fill={`url(#grid${uid})`} />
        <g>
          {TYRES.map((t, i) => (
            <rect key={i} x={t.x} y={t.y} width={t.w} height={t.h} rx="6" {...shape('Wheels or tires')} />
          ))}
        </g>
        <path d="M30 144 C18 140 10 148 12 157 C18 162 26 160 31 156 Z" {...shape('Left side')} />
        <path d="M 190 144 C 202 140 210 148 208 157 C 202 162 194 160 189 156 Z" {...shape('Right side')} />
        <path
          d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z"
          fill="#ffffff"
          stroke={inkStroke}
          strokeWidth={1.5}
        />
        <g clipPath={`url(#clip${uid})`}>
          {HL.map((h) =>
            h.d ? (
              <path key={h.area} d={h.d} {...shape(h.area)} />
            ) : (
              <rect key={h.area} x={h.x} y={h.y} width={h.w} height={h.h} {...shape(h.area)} />
            ),
          )}
        </g>
        <g fill="none" stroke={inkStroke} strokeWidth={2.2} strokeLinejoin="round" strokeLinecap="round" pointerEvents="none">
          <path d="M110 14 C150 14 176 22 186 44 C194 62 196 84 196 104 C196 130 192 150 191 170 L191 300 C192 320 196 340 196 362 C196 392 192 414 184 428 C174 444 148 448 110 448 C72 448 46 444 36 428 C28 414 24 392 24 362 C24 340 28 320 29 300 L29 170 C28 150 24 130 24 104 C24 84 26 62 34 44 C44 22 70 14 110 14 Z" />
          <path d="M58 152 C90 142 130 142 162 152 L152 200 C126 194 94 194 68 200 Z" />
          <path d="M68 202 C94 196 126 196 152 202 L154 318 C128 324 92 324 66 318 Z" />
          <path d="M66 320 C92 326 128 326 154 320 L164 362 C130 372 90 372 56 362 Z" />
          <path d="M56 364 C90 374 130 374 164 364 L166 424 C130 432 90 432 54 424 Z" />
          <path d="M52 42 C54 80 56 120 58 150" />
          <path d="M 168 42 C 166 80 164 120 162 150" />
          <path d="M84 50 C88 90 90 120 88 146" strokeWidth={1.2} opacity={0.7} />
          <path d="M 136 50 C 132 90 130 120 132 146" strokeWidth={1.2} opacity={0.7} />
          <path d="M40 38 C70 30 150 30 180 38" strokeWidth={1.2} opacity={0.7} />
          <path d="M44 424 C70 432 150 432 176 424" strokeWidth={1.2} opacity={0.7} />
          <path d="M36 172 C40 162 50 158 60 158 L64 248 L34 248 Z" />
          <path d="M34 254 L64 254 L65 314 C55 314 43 310 36 300 Z" />
          <path d="M 184 172 C 180 162 170 158 160 158 L 156 248 L 186 248 Z" />
          <path d="M 186 254 L 156 254 L 155 314 C 165 314 177 310 184 300 Z" />
          <path d="M31 172 C33 220 33 270 31 330" strokeWidth={1.2} opacity={0.7} />
          <path d="M 189 172 C 187 220 187 270 189 330" strokeWidth={1.2} opacity={0.7} />
          <path d="M24 251 L34 251" />
          <path d="M186 251 L196 251" />
          <path d="M38 40 C42 28 56 22 74 22 L76 32 C62 34 50 38 44 48 Z" />
          <path d="M 182 40 C 178 28 164 22 146 22 L 144 32 C 158 34 170 38 176 48 Z" />
          <path d="M38 424 C44 436 58 442 74 442 L75 434 C61 433 50 429 45 420 Z" stroke="#d9544a" />
          <path d="M 182 424 C 176 436 162 442 146 442 L 145 434 C 159 433 170 429 175 420 Z" stroke="#d9544a" />
          <path d="M86 160 L98 150 M92 172 L110 156 M128 172 L140 162" strokeWidth={1.2} opacity={0.7} />
          <path d="M84 336 L96 328 M120 344 L138 332" strokeWidth={1.2} opacity={0.7} />
          <path d="M92 20 C104 18 116 18 128 20" strokeWidth={1.2} opacity={0.7} />
        </g>
        <g fill="var(--ink)" fontSize={13} fontWeight={800} textAnchor="middle" pointerEvents="none">
          {HL.map((h) =>
            markersByKey[norm(h.area)] ? (
              <text key={h.area} x={h.cx} y={h.cy + 4} stroke="#fff" strokeWidth={3} paintOrder="stroke">
                {markersByKey[norm(h.area)]}
              </text>
            ) : null,
          )}
          {markersByKey[norm('Wheels or tires')]
            ? TYRE_CENTRES.map((c, i) => (
                <text key={i} x={c.x} y={c.y + 4} fontSize={11} stroke="#fff" strokeWidth={3} paintOrder="stroke">
                  {markersByKey[norm('Wheels or tires')]}
                </text>
              ))
            : null}
        </g>
        <text x="110" y="-10" fontSize={11} fontWeight={600} fill="var(--ink-3)" textAnchor="middle" letterSpacing="0.04em" pointerEvents="none">FRONT</text>
        <text x="-20" y="236" fontSize={11} fontWeight={600} fill="var(--ink-3)" textAnchor="middle" transform="rotate(-90 -20 236)" pointerEvents="none">Driver&apos;s side (left)</text>
        <text x="240" y="236" fontSize={11} fontWeight={600} fill="var(--ink-3)" textAnchor="middle" transform="rotate(90 240 236)" pointerEvents="none">Passenger side (right)</text>
      </svg>
    </div>
  )
}

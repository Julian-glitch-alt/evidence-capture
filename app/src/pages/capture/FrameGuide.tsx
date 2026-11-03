/** Framing guide drawn over the live camera, one shape per slot. Pure CSS and SVG; it only guides, it checks nothing. */

import { GUIDES } from './frameGuides'

const MASK = '0 0 0 9999px rgba(0,0,0,0.38)'

export default function FrameGuide({ slotId }: { slotId: string }) {
  const g = GUIDES[slotId]
  if (!g) return null
  return (
    <div aria-hidden="true" data-testid="frame-guide" className="pointer-events-none absolute inset-0 flex items-center justify-center overflow-hidden">
      {g.kind === 'box' && (
        <div style={{ width: g.width, aspectRatio: g.aspect, borderRadius: g.radius, boxShadow: MASK }} className="border-[3px] border-white" />
      )}
      {g.kind === 'dash' && (
        // A dashboard cluster: an arched hood over the gauges, with the mileage window low in the middle.
        <div style={{ width: g.width, aspectRatio: g.aspect, borderRadius: '50% 50% 14px 14px / 70% 70% 14px 14px', boxShadow: MASK }} className="relative border-[3px] border-white">
          <svg viewBox="0 0 220 100" className="absolute inset-0 h-full w-full">
            <path d="M40 78 A38 38 0 0 1 104 50" fill="none" stroke="white" strokeOpacity="0.7" strokeWidth="2.5" strokeDasharray="4 5" />
            <path d="M116 50 A38 38 0 0 1 180 78" fill="none" stroke="white" strokeOpacity="0.7" strokeWidth="2.5" strokeDasharray="4 5" />
            <rect x="78" y="66" width="64" height="20" rx="4" fill="none" stroke="white" strokeWidth="2.5" />
          </svg>
        </div>
      )}
      {g.kind === 'corners' && (
        <div style={{ width: g.width, aspectRatio: g.aspect, boxShadow: MASK }} className="relative">
          {['left-0 top-0 border-l-4 border-t-4 rounded-tl-xl', 'right-0 top-0 border-r-4 border-t-4 rounded-tr-xl',
            'bottom-0 left-0 border-b-4 border-l-4 rounded-bl-xl', 'bottom-0 right-0 border-b-4 border-r-4 rounded-br-xl'].map((c) => (
            <span key={c} className={`absolute h-10 w-10 border-white ${c}`} />
          ))}
        </div>
      )}
    </div>
  )
}

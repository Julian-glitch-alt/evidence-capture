import CarDiagram, { type CarAreaState } from './CarDiagram'

export function normalizeArea(area: string) {
  return area.trim().toLowerCase()
}

const OFF_MAP = ['Interior', 'Underneath']

/** The adjuster's damage view: the same car the claimant saw, with the damage
 *  read from the photos lit on it (section 4 of tasks/t45-design.md). */
export default function DamageMap({
  seenByZone,
  declaredZones,
  markersByZone,
  selected,
  onSelect,
}: {
  seenByZone: Record<string, number>
  declaredZones: string[]
  markersByZone: Record<string, (string | number)[]>
  selected: string | null
  onSelect: (area: string | null) => void
}) {
  const declared = new Set(declaredZones.map(normalizeArea))
  const states: Record<string, CarAreaState> = {}
  for (const key of new Set([...Object.keys(seenByZone), ...declared])) {
    states[key] = { seen: seenByZone[key] ?? 0, declared: declared.has(key) }
  }
  const markers: Record<string, string> = {}
  for (const [key, values] of Object.entries(markersByZone)) {
    markers[key] = [...new Set(values.map(String))].join(', ')
  }
  return (
    <div className="grid gap-2">
      <CarDiagram
        states={states}
        markers={markers}
        active={selected}
        onArea={(area) => onSelect(selected != null && normalizeArea(selected) === normalizeArea(area) ? null : area)}
      />
      <p className="m-0 text-center text-[11.5px] text-[var(--ink-3)]">
        Yellow: damage seen · solid outline: the claimant listed it · dashed: seen, not listed · red: listed, not seen
      </p>
      <div className="flex flex-wrap justify-center gap-2" aria-label="Damages outside the top view">
        {OFF_MAP.map((name) => {
          const key = normalizeArea(name)
          const seen = seenByZone[key] ?? 0
          const isDeclared = declared.has(key)
          if (seen === 0 && !isDeclared) return null
          const isSelected = selected != null && normalizeArea(selected) === key
          const markerList = markersByZone[key] ?? []
          return (
            <button
              key={name}
              type="button"
              aria-pressed={isSelected}
              aria-label={`${name}: ${seen} seen, ${isDeclared ? 'declared' : 'not declared'}. Activate to filter.`}
              onClick={() => onSelect(isSelected ? null : name)}
              className="rounded-sm border border-[var(--rule)] bg-white px-2 py-1 text-[11.5px] text-[var(--ink)]"
              style={isSelected ? { outline: '2px solid var(--ink)' } : undefined}
            >
              {name}
              {markerList.length > 0 ? ` · ${[...new Set(markerList.map(String))].join(', ')}` : ` · ${seen} seen`}
            </button>
          )
        })}
      </div>
      <p className="m-0 text-[11.5px] text-[var(--ink-3)]">
        Top view, front at the top. Click a part to filter the damage table and the photo grid; click again to
        clear.
      </p>
    </div>
  )
}

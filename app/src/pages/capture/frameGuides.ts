/** Framing guide per slot: the shape drawn over the live camera and its one-line instruction. */

export type Shape = { kind: 'box' | 'dash' | 'corners'; width: string; aspect: number; radius?: string; line: string }

export const GUIDES: Record<string, Shape> = {
  plate: { kind: 'box', width: '78%', aspect: 2, radius: '12px', line: 'Fit the whole plate inside the box.' },
  vin_photo: { kind: 'box', width: '90%', aspect: 7, radius: '6px', line: 'Line up all 17 characters inside the strip.' },
  odometer: { kind: 'dash', width: '86%', aspect: 2.2, line: 'Fit the mileage display inside the dashboard outline.' },
  driver_license: { kind: 'box', width: '84%', aspect: 1.586, radius: '14px', line: 'Lay the license flat and fit it inside the card.' },
  documents: { kind: 'box', width: '72%', aspect: 8.5 / 11, radius: '4px', line: 'Fit the whole page inside the outline.' },
  damage_closeup: { kind: 'corners', width: '64%', aspect: 1, line: 'Get close: fill the corners with the damaged area.' },
  damage_wide: { kind: 'corners', width: '90%', aspect: 4 / 3, line: 'Step back: the damage and the panels around it.' },
  walkaround: { kind: 'corners', width: '92%', aspect: 4 / 3, line: 'Keep the whole vehicle inside the corners as you walk.' },
}

/** The one-line instruction shown with the guide; falls back to the slot's own text. */
export function guideLine(slotId: string, fallback: string) {
  return GUIDES[slotId]?.line ?? fallback
}

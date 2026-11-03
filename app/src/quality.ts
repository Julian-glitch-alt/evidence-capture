/** On-device photo quality gate (T34b): blur and brightness before upload. */

export const QUALITY_VERSION = 'quality/v1'
export const MAX_QUALITY_FAILS = 3

export type Quality = { sharpness: number; brightness: number }
export type Thresholds = { min_sharpness: number; min_brightness: number; max_brightness: number }
export type QualityProblem = 'too_dark' | 'too_bright' | 'blurry'

export const QUALITY_HINTS: Record<QualityProblem, string> = {
  blurry: 'The photo is blurry. Hold the phone still and tap the screen to focus.',
  too_dark: 'The photo is too dark. Move to a brighter spot or turn on more light.',
  too_bright: 'The photo is too bright. Move out of direct light or tilt the phone.',
}

function round1(v: number): number {
  return Math.round(v * 10) / 10
}

/** Brightness is the mean grey value; sharpness the variance of the Laplacian over interior pixels. */
export function measureGray(gray: ArrayLike<number>, width: number, height: number): Quality {
  const n = width * height
  let sum = 0
  for (let i = 0; i < n; i++) sum += gray[i]
  const brightness = n ? sum / n : 0
  let sharpness = 0
  if (width >= 3 && height >= 3) {
    const lap = (x: number, y: number) =>
      gray[(y - 1) * width + x] + gray[(y + 1) * width + x] + gray[y * width + x - 1] + gray[y * width + x + 1] - 4 * gray[y * width + x]
    const count = (width - 2) * (height - 2)
    let mean = 0
    for (let y = 1; y < height - 1; y++) for (let x = 1; x < width - 1; x++) mean += lap(x, y)
    mean /= count
    let variance = 0
    for (let y = 1; y < height - 1; y++) for (let x = 1; x < width - 1; x++) variance += (lap(x, y) - mean) ** 2
    sharpness = variance / count
  }
  return { sharpness: round1(sharpness), brightness: round1(brightness) }
}

/** Downscale so the longer side is 512 px (never upscale), then measure the grey values. */
export function measure(source: CanvasImageSource, width: number, height: number): Quality {
  const scale = Math.max(width, height) > 512 ? 512 / Math.max(width, height) : 1
  const w = Math.max(1, Math.round(width * scale))
  const h = Math.max(1, Math.round(height * scale))
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Could not measure photo quality')
  ctx.drawImage(source, 0, 0, w, h)
  const data = ctx.getImageData(0, 0, w, h).data
  const gray = new Float64Array(w * h)
  for (let i = 0; i < w * h; i++) gray[i] = 0.299 * data[i * 4] + 0.587 * data[i * 4 + 1] + 0.114 * data[i * 4 + 2]
  return measureGray(gray, w, h)
}

export function judge(q: Quality, t: Thresholds): QualityProblem | null {
  if (q.brightness < t.min_brightness) return 'too_dark'
  if (q.brightness > t.max_brightness) return 'too_bright'
  if (q.sharpness < t.min_sharpness) return 'blurry'
  return null
}

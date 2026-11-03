import { useCallback, useEffect, useRef, useState } from 'react'
import { measure, type Quality } from './quality'

export type CameraStatus = 'idle' | 'starting' | 'ready' | 'denied' | 'unavailable'

const VIDEO_TYPES = ['video/mp4;codecs=avc1', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm']

/** Live rear camera only. There is deliberately no way to pick an existing file. */
export function useCamera() {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [status, setStatus] = useState<CameraStatus>('idle')
  const [error, setError] = useState<string | null>(null)

  const attach = useCallback((el: HTMLVideoElement | null) => {
    videoRef.current = el
    if (el && streamRef.current && el.srcObject !== streamRef.current) {
      el.srcObject = streamRef.current
      el.play().catch(() => {})
    }
  }, [])

  const start = useCallback(async () => {
    if (streamRef.current) return
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus('unavailable')
      setError('This browser cannot open the camera. Open the link in Safari (iPhone) or Chrome (Android).')
      return
    }
    setStatus('starting')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      })
      streamRef.current = stream
      attach(videoRef.current)
      setStatus('ready')
    } catch (err) {
      const name = err instanceof DOMException ? err.name : ''
      setStatus(name === 'NotAllowedError' ? 'denied' : 'unavailable')
      setError(
        name === 'NotAllowedError'
          ? 'Camera access was blocked. Allow camera access for this site in your browser settings, then reload.'
          : 'No camera could be opened on this device.',
      )
    }
  }, [attach])

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    setStatus('idle')
  }, [])

  useEffect(() => stop, [stop])

  const trackInfo = useCallback(() => {
    const track = streamRef.current?.getVideoTracks()[0]
    const s = track?.getSettings()
    return {
      camera_label: track?.label ?? null,
      width: s?.width ?? null,
      height: s?.height ?? null,
      facing_mode: s?.facingMode ?? null,
      frame_rate: s?.frameRate ?? null,
      user_agent: navigator.userAgent,
    }
  }, [])

  const takePhoto = useCallback(async (): Promise<{ blob: Blob; meta: Record<string, unknown>; quality: Quality | null }> => {
    const video = videoRef.current
    if (!video || !video.videoWidth) throw new Error('Camera is not ready yet')
    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    canvas.getContext('2d')!.drawImage(video, 0, 0)
    let quality: Quality | null = null
    try {
      quality = measure(canvas, canvas.width, canvas.height)
    } catch {
      // a browser that cannot measure skips the gate rather than losing the shot
    }
    const blob = await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('Could not capture photo'))), 'image/jpeg', 0.9),
    )
    return { blob, meta: { ...trackInfo(), captured_at: new Date().toISOString() }, quality }
  }, [trackInfo])

  const recordVideo = useCallback(
    (maxSeconds: number, onTick: (seconds: number) => void) => {
      const stream = streamRef.current
      if (!stream) throw new Error('Camera is not ready yet')
      const mimeType = VIDEO_TYPES.find((t) => MediaRecorder.isTypeSupported(t)) ?? ''
      const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 2_500_000 })
      const chunks: Blob[] = []
      const startedAt = Date.now()
      const startedIso = new Date().toISOString()
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data)
      const tick = setInterval(() => {
        const s = (Date.now() - startedAt) / 1000
        onTick(s)
        if (s >= maxSeconds && recorder.state === 'recording') recorder.stop()
      }, 200)
      const done = new Promise<{ blob: Blob; meta: Record<string, unknown> }>((resolve) => {
        recorder.onstop = () => {
          clearInterval(tick)
          const type = (recorder.mimeType || mimeType || 'video/mp4').split(';')[0]
          resolve({
            blob: new Blob(chunks, { type }),
            meta: { ...trackInfo(), captured_at: startedIso, duration_ms: Date.now() - startedAt },
          })
        }
      })
      recorder.start(1000)
      return { stop: () => recorder.state === 'recording' && recorder.stop(), done }
    },
    [trackInfo],
  )

  return { attach, start, stop, status, error, takePhoto, recordVideo }
}

export function isLikelyDesktop() {
  return !window.matchMedia('(pointer: coarse)').matches
}

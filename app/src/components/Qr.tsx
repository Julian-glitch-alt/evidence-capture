import QRCode from 'qrcode'
import { useEffect, useState } from 'react'

export default function Qr({ value, size = 192 }: { value: string; size?: number }) {
  const [src, setSrc] = useState<string>()
  useEffect(() => {
    QRCode.toDataURL(value, { width: size * 2, margin: 1 }).then(setSrc)
  }, [value, size])
  return src ? (
    <img src={src} width={size} height={size} alt="QR code for the capture link" className="rounded-lg bg-white" />
  ) : (
    <div style={{ width: size, height: size }} className="rounded-lg bg-slate-100" />
  )
}

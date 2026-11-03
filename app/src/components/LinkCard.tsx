import { useState } from 'react'
import { captureLink } from '../api'
import Qr from './Qr'

export default function LinkCard({ token, claimId }: { token: string; claimId: string }) {
  const url = captureLink(token)
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(url)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex flex-col gap-5 rounded-xl border border-indigo-200 bg-indigo-50 p-5 sm:flex-row sm:items-center">
      <Qr value={url} size={140} />
      <div className="min-w-0 flex-1 space-y-3">
        <div>
          <p className="text-sm font-medium text-indigo-900">Capture link for {claimId}</p>
          <p className="text-sm text-indigo-800/80">Send it to the customer, or let them scan the QR code with their phone.</p>
        </div>
        <div className="flex gap-2">
          <input readOnly value={url} className="min-w-0 flex-1 rounded-lg border border-indigo-200 bg-white px-3 py-2 text-sm" />
          <button onClick={copy} className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white">
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>
    </div>
  )
}

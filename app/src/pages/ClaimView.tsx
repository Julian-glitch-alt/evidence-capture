import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, type ClaimDetail } from '../api'
import AdminLayout from '../components/AdminLayout'
import ClaimWorkspace from './claim/ClaimWorkspace'

export default function ClaimView() {
  const { claimId = '' } = useParams()
  const [claim, setClaim] = useState<ClaimDetail>()
  const [error, setError] = useState<string>()

  const refreshing =
    claim == null ||
    claim.decision == null ||
    claim.damage?.status === 'pending' ||
    claim.consistency?.status === 'pending' ||
    claim.proposal?.status === 'pending'

  useEffect(() => {
    if (!claimId) return
    let cancelled = false
    const load = () =>
      api
        .claim(claimId)
        .then((c) => {
          if (!cancelled) setClaim(c)
        })
        .catch((e) => {
          if (!cancelled) setError(e instanceof Error ? e.message : String(e))
        })
    void load()
    const timer = setInterval(load, refreshing ? 3000 : 5000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [claimId, refreshing])

  if (error)
    return (
      <AdminLayout>
        <p className="mute" style={{ padding: '16px 6px' }}>
          {error}
        </p>
      </AdminLayout>
    )
  if (!claim)
    return (
      <AdminLayout>
        <p className="mute" style={{ padding: '32px 6px' }}>
          Loading…
        </p>
      </AdminLayout>
    )
  return (
    <AdminLayout>
      <ClaimWorkspace claim={claim} onUpdate={setClaim} />
    </AdminLayout>
  )
}

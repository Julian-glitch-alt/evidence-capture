import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { PolicyOption } from '../../api'
import { demoApi } from '../../demoApi'
import DemoLayout from './DemoLayout'

export default function NewClaim() {
  const navigate = useNavigate()
  const [policies, setPolicies] = useState<PolicyOption[]>([])
  const [policyNo, setPolicyNo] = useState('')
  const [lossDate, setLossDate] = useState('2026-10-27')
  const [lossCause, setLossCause] = useState('collision')
  const [description, setDescription] = useState('')
  const [phone, setPhone] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    demoApi
      .policies()
      .then((list) => {
        setPolicies(list)
        if (list.length > 0) setPolicyNo((prev) => prev || list[0].policy_no)
      })
      .catch((e) => setError(e.message))
  }, [])

  const selected = policies.find((p) => p.policy_no === policyNo)

  const save = async () => {
    setSaving(true)
    setError(undefined)
    try {
      const claim = await demoApi.createClaim({
        policy_number: policyNo,
        loss_date: lossDate,
        loss_cause: lossCause,
        description,
        claimant_phone: phone || null,
      })
      navigate(`/demo-insurer/claims/${claim.claim_number}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not open the claim')
      setSaving(false)
    }
  }

  return (
    <DemoLayout>
      <h1 className="mb-3 text-base font-semibold">New claim (first notice of loss)</h1>
      {error && <p className="mb-3 text-red-700">{error}</p>}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="rounded-sm border border-slate-300 bg-white p-3">
          <h2 className="mb-2 font-semibold text-slate-600">1 · Find policy</h2>
          <label className="block text-[12px] text-slate-500">
            Policy
            <select
              value={policyNo}
              onChange={(e) => setPolicyNo(e.target.value)}
              className="mt-1 block w-full rounded-sm border border-slate-300 px-2 py-1.5"
            >
              {policies.map((p) => (
                <option key={p.policy_no} value={p.policy_no}>
                  {p.policy_no} · {p.holder} · {p.vehicle}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <dl className="mt-2 grid grid-cols-2 gap-1 text-[12px]">
              <dt className="text-slate-500">Holder</dt>
              <dd>{selected.holder}</dd>
              <dt className="text-slate-500">Vehicle</dt>
              <dd>{selected.vehicle}</dd>
              <dt className="text-slate-500">State</dt>
              <dd>{selected.state}</dd>
              <dt className="text-slate-500">Expires</dt>
              <dd>{selected.expires}</dd>
            </dl>
          )}
        </section>

        <section className="rounded-sm border border-slate-300 bg-white p-3">
          <h2 className="mb-2 font-semibold text-slate-600">2 · Loss details</h2>
          <div className="space-y-2">
            <label className="block text-[12px] text-slate-500">
              Loss date
              <input
                type="date"
                value={lossDate}
                onChange={(e) => setLossDate(e.target.value)}
                className="mt-1 block w-full rounded-sm border border-slate-300 px-2 py-1.5 text-slate-800"
              />
            </label>
            <label className="block text-[12px] text-slate-500">
              Cause
              <select
                value={lossCause}
                onChange={(e) => setLossCause(e.target.value)}
                className="mt-1 block w-full rounded-sm border border-slate-300 px-2 py-1.5 text-slate-800"
              >
                <option value="collision">Collision</option>
                <option value="comprehensive">Comprehensive</option>
              </select>
            </label>
            <label className="block text-[12px] text-slate-500">
              Description
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="mt-1 block w-full rounded-sm border border-slate-300 px-2 py-1.5 text-slate-800"
              />
            </label>
            <label className="block text-[12px] text-slate-500">
              Claimant phone
              <input
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+1 415 555 0101"
                className="mt-1 block w-full rounded-sm border border-slate-300 px-2 py-1.5 text-slate-800"
              />
            </label>
            <button
              onClick={save}
              disabled={saving || !policyNo || !description}
              className="rounded-sm bg-slate-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-slate-600 disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save claim'}
            </button>
          </div>
        </section>
      </div>
    </DemoLayout>
  )
}

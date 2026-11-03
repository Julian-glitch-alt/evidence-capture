import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { demoApi, type DemoDesktop } from '../../demoApi'
import DemoLayout from './DemoLayout'

export default function Desktop() {
  const [desk, setDesk] = useState<DemoDesktop>()
  const [error, setError] = useState<string>()

  useEffect(() => {
    demoApi.desktop().then(setDesk).catch((e) => setError(e.message))
  }, [])

  return (
    <DemoLayout>
      <div className="mb-3 flex items-center justify-between">
        <h1 className="text-base font-semibold">Desktop</h1>
        <Link
          to="/demo-insurer/new"
          className="rounded-sm bg-slate-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-slate-600"
        >
          New claim
        </Link>
      </div>
      {error && <p className="mb-3 text-red-700">{error}</p>}
      {!desk && !error && <p className="text-slate-500">Loading…</p>}
      {desk && (
        <>
          <section className="mb-4">
            <h2 className="mb-1 text-[13px] font-semibold text-slate-600">
              My Activities ({desk.activities.length})
            </h2>
            <div className="overflow-hidden rounded-sm border border-slate-300 bg-white">
              <table className="w-full text-left">
                <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
                  <tr>
                    <th className="px-2 py-1 font-medium">Subject</th>
                    <th className="px-2 py-1 font-medium">Claim</th>
                    <th className="px-2 py-1 font-medium">Due</th>
                    <th className="px-2 py-1 font-medium">Priority</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {desk.activities.length === 0 && (
                    <tr>
                      <td colSpan={4} className="px-2 py-4 text-center text-slate-500">
                        No open activities.
                      </td>
                    </tr>
                  )}
                  {desk.activities.map((a) => (
                    <tr key={a.id} className="hover:bg-slate-50">
                      <td className="px-2 py-1">{a.subject}</td>
                      <td className="px-2 py-1">
                        <Link
                          to={`/demo-insurer/claims/${a.claim_number}`}
                          className="text-slate-700 underline"
                        >
                          {a.claim_number}
                        </Link>
                      </td>
                      <td className="px-2 py-1 tabular-nums">{a.due_date}</td>
                      <td className="px-2 py-1">{a.priority}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section>
            <h2 className="mb-1 text-[13px] font-semibold text-slate-600">
              My Claims ({desk.claims.length})
            </h2>
            <div className="overflow-hidden rounded-sm border border-slate-300 bg-white">
              <table className="w-full text-left">
                <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
                  <tr>
                    <th className="px-2 py-1 font-medium">Number</th>
                    <th className="px-2 py-1 font-medium">Insured</th>
                    <th className="px-2 py-1 font-medium">Loss date</th>
                    <th className="px-2 py-1 font-medium">Status</th>
                    <th className="px-2 py-1 font-medium">Verified capture</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {desk.claims.length === 0 && (
                    <tr>
                      <td colSpan={5} className="px-2 py-4 text-center text-slate-500">
                        No claims yet. Click{' '}
                        <Link to="/demo-insurer/new" className="text-slate-700 underline">
                          New claim
                        </Link>{' '}
                        to open one, then order a verified capture from the claim.
                      </td>
                    </tr>
                  )}
                  {desk.claims.map((c) => (
                    <tr key={c.claim_number} className="hover:bg-slate-50">
                      <td className="px-2 py-1 font-medium">
                        <Link
                          to={`/demo-insurer/claims/${c.claim_number}`}
                          className="text-slate-700 underline"
                        >
                          {c.claim_number}
                        </Link>
                      </td>
                      <td className="px-2 py-1">{c.insured ?? '—'}</td>
                      <td className="px-2 py-1 tabular-nums">{c.loss_date}</td>
                      <td className="px-2 py-1">{c.status}</td>
                      <td className="px-2 py-1">{c.service_status ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </DemoLayout>
  )
}

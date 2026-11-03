import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import { demoApi, type DemoClaim } from '../../demoApi'
import Qr from '../../components/Qr'
import DemoLayout from './DemoLayout'

type Tab =
  | 'Summary'
  | 'Exposures'
  | 'Parties'
  | 'Policy'
  | 'Financials'
  | 'Workplan'
  | 'Notes'
  | 'Documents'
  | 'Services'

const TABS: Tab[] = [
  'Summary',
  'Exposures',
  'Parties',
  'Policy',
  'Financials',
  'Workplan',
  'Notes',
  'Documents',
  'Services',
]

export default function ClaimPage() {
  const { claimNumber = '' } = useParams()
  const [claim, setClaim] = useState<DemoClaim>()
  const [error, setError] = useState<string>()
  const [tab, setTab] = useState<Tab>('Summary')

  const load = useCallback(() => {
    demoApi.claim(claimNumber).then(setClaim).catch((e) => setError(e.message))
  }, [claimNumber])

  useEffect(() => {
    load()
  }, [load])

  const requested = claim?.services.some((s) => s.status === 'Requested') ?? false
  useEffect(() => {
    if (!requested) return
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [requested, load])

  return (
    <DemoLayout>
      {error && <p className="mb-3 text-red-700">{error}</p>}
      {!claim && !error && <p className="text-slate-500">Loading…</p>}
      {claim && (
        <>
          <div className="mb-3 rounded-sm border border-slate-300 bg-white p-3">
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
              <h1 className="text-base font-semibold">{claim.claim_number}</h1>
              <StatusChip status={claim.status} />
              <span className="text-[12px] text-slate-500">
                {claim.insured_name} · {claim.vehicle.vehicle} · loss {claim.loss_date}
              </span>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-[160px_1fr]">
            <nav className="flex flex-row flex-wrap gap-1 lg:flex-col">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`rounded-sm px-2 py-1 text-left text-[12px] ${
                    tab === t
                      ? 'bg-slate-700 font-medium text-white'
                      : 'bg-white text-slate-600 hover:bg-slate-100'
                  }`}
                >
                  {t}
                  {t === 'Notes' && claim.notes.length > 0 && ` (${claim.notes.length})`}
                  {t === 'Documents' && claim.documents.length > 0 && ` (${claim.documents.length})`}
                </button>
              ))}
            </nav>

            <div className="min-w-0">
              {tab === 'Summary' && <SummaryTab claim={claim} reload={load} />}
              {tab === 'Exposures' && <ExposuresTab claim={claim} reload={load} />}
              {tab === 'Parties' && <PartiesTab claim={claim} />}
              {tab === 'Policy' && <PolicyTab claim={claim} />}
              {tab === 'Financials' && <FinancialsTab claim={claim} reload={load} />}
              {tab === 'Workplan' && <WorkplanTab claim={claim} reload={load} />}
              {tab === 'Notes' && <NotesTab claim={claim} reload={load} />}
              {tab === 'Documents' && <DocumentsTab claim={claim} />}
              {tab === 'Services' && <ServicesPanel claim={claim} reload={load} full />}
            </div>
          </div>
        </>
      )}
    </DemoLayout>
  )
}

function StatusChip({ status }: { status: string }) {
  return (
    <span className="rounded-sm bg-slate-200 px-1.5 py-0.5 text-[11px] font-medium text-slate-700">
      {status}
    </span>
  )
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mb-3 rounded-sm border border-slate-300 bg-white p-3">
      <h2 className="mb-2 text-[13px] font-semibold text-slate-600">{title}</h2>
      {children}
    </section>
  )
}

function SummaryTab({ claim, reload }: { claim: DemoClaim; reload: () => void }) {
  return (
    <>
      <Card title="Loss details">
        <dl className="grid grid-cols-2 gap-1 text-[12px] sm:grid-cols-4">
          <dt className="text-slate-500">Loss date</dt>
          <dd className="tabular-nums">{claim.loss_date}</dd>
          <dt className="text-slate-500">Cause</dt>
          <dd className="capitalize">{claim.loss_cause}</dd>
          <dt className="text-slate-500">Policy</dt>
          <dd>{claim.policy_number}</dd>
          <dt className="text-slate-500">Phone</dt>
          <dd>{claim.claimant_phone ?? '—'}</dd>
        </dl>
        <p className="mt-2 text-[12px]">{claim.description}</p>
      </Card>

      <Card title="Exposures">
        <ExposureTable claim={claim} />
      </Card>

      <Card title="High-risk indicators">
        {claim.flags.length === 0 ? (
          <p className="text-[12px] text-slate-500">None.</p>
        ) : (
          <ul className="space-y-1">
            {claim.flags.map((f) => (
              <li key={f.id} className="flex items-start gap-2 text-[12px]">
                <span className="font-bold text-red-700">{flagMark(f.level)}</span>
                <span>
                  {f.reason}
                  {f.evidence.length > 0 && (
                    <span className="ml-2 text-slate-500">[{f.evidence.join('; ')}]</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Financials">
        <FinancialTotals claim={claim} />
      </Card>

      <ServicesPanel claim={claim} reload={reload} />
    </>
  )
}

function flagMark(level: string) {
  return level === 'conflict' ? '!' : level === 'review' ? '?' : 'i'
}

function ExposureTable({ claim }: { claim: DemoClaim }) {
  return (
    <table className="w-full text-left text-[12px]">
      <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
        <tr>
          <th className="px-2 py-1 font-medium">Type</th>
          <th className="px-2 py-1 font-medium">Coverage</th>
          <th className="px-2 py-1 font-medium">Claimant</th>
          <th className="px-2 py-1 font-medium">Reserve</th>
          <th className="px-2 py-1 font-medium">Status</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-slate-200">
        {claim.exposures.map((e) => (
          <tr key={e.id}>
            <td className="px-2 py-1">{e.type}</td>
            <td className="px-2 py-1">{e.coverage}</td>
            <td className="px-2 py-1">{e.claimant ?? '—'}</td>
            <td className="px-2 py-1 tabular-nums">${e.reserve.toLocaleString('en-US')}</td>
            <td className="px-2 py-1">{e.status}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function FinancialTotals({ claim }: { claim: DemoClaim }) {
  const usd = (n: number) => `$${n.toLocaleString('en-US')}`
  return (
    <dl className="grid grid-cols-3 gap-1 text-[12px]">
      <dt className="text-slate-500">Reserves</dt>
      <dd className="tabular-nums">{usd(claim.financials.reserves)}</dd>
      <dt className="text-slate-500">Paid</dt>
      <dd className="tabular-nums">{usd(claim.financials.paid)}</dd>
      <dt className="text-slate-500">Recovered</dt>
      <dd className="tabular-nums">{usd(claim.financials.recovered)}</dd>
    </dl>
  )
}

function ServicesPanel({
  claim,
  reload,
  full,
}: {
  claim: DemoClaim
  reload: () => void
  full?: boolean
}) {
  const [requesting, setRequesting] = useState(false)
  const [error, setError] = useState<string>()

  const request = async () => {
    setRequesting(true)
    setError(undefined)
    try {
      await demoApi.requestCapture(claim.claim_number)
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed')
    } finally {
      setRequesting(false)
    }
  }

  const body = (
    <>
      {error && <p className="mb-2 text-red-700">{error}</p>}
      {claim.services.length === 0 ? (
        <button
          onClick={request}
          disabled={requesting}
          className="rounded-sm bg-slate-700 px-3 py-1.5 text-[12px] font-medium text-white hover:bg-slate-600 disabled:opacity-50"
        >
          {requesting ? 'Requesting…' : 'Request verified capture'}
        </button>
      ) : (
        claim.services.map((s) => <ServiceRow key={s.id} serviceId={s.id} claim={claim} />)
      )}
    </>
  )
  if (full) return <Card title="Services">{body}</Card>
  return (
    <section className="mb-3 rounded-sm border border-slate-300 bg-white p-3">
      <h2 className="mb-2 text-[13px] font-semibold text-slate-600">Services</h2>
      {body}
    </section>
  )
}

function ServiceRow({ serviceId, claim }: { serviceId: string; claim: DemoClaim }) {
  const service = claim.services.find((s) => s.id === serviceId)
  const [copied, setCopied] = useState(false)
  if (!service) return null
  const nav = navigator as Navigator & { share?: (data: ShareData) => Promise<void> }
  const canShare = typeof nav.share === 'function'
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(service.capture_url ?? '')
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }
  const share = () => {
    nav.share?.({ title: 'Capture link', url: service.capture_url ?? '' }).catch(() => {})
  }
  return (
    <div className="rounded-sm border border-slate-200 p-2">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[12px]">
        <span className="font-medium">{service.type}</span>
        <StatusChip status={service.status} />
        <span className="text-slate-500">Vendor: {service.vendor}</span>
        {service.vendor_ref && (
          <span className="text-slate-500">
            Vendor ref: <span className="font-mono">{service.vendor_ref}</span>
          </span>
        )}
      </div>
      {service.capture_url && (
        <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center">
          <Qr value={service.capture_url} size={140} />
          <div className="min-w-0 flex-1 space-y-2">
            <input
              readOnly
              value={service.capture_url}
              className="w-full rounded-sm border border-slate-300 bg-white px-2 py-1 text-[12px]"
            />
            <div className="flex gap-2">
              <button
                onClick={copy}
                className="rounded-sm border border-slate-300 px-3 py-1 text-[12px] font-medium hover:bg-slate-50"
              >
                {copied ? 'Copied' : 'Copy'}
              </button>
              {canShare && (
                <button
                  onClick={share}
                  className="rounded-sm border border-slate-300 px-3 py-1 text-[12px] font-medium hover:bg-slate-50"
                >
                  Share
                </button>
              )}
            </div>
            <p className="text-[11px] text-slate-500">SMS sent (simulated)</p>
          </div>
        </div>
      )}
    </div>
  )
}

function ExposuresTab({ claim, reload }: { claim: DemoClaim; reload: () => void }) {
  return (
    <Card title="Exposures">
      <ExposureTable claim={claim} />
      <div className="mt-3 space-y-2">
        {claim.exposures.map((e) => (
          <ReserveRow key={e.id} exposureId={e.id} type={e.type} reserve={e.reserve} reload={reload} />
        ))}
      </div>
    </Card>
  )
}

function ReserveRow({
  exposureId,
  type,
  reserve,
  reload,
}: {
  exposureId: string
  type: string
  reserve: number
  reload: () => void
}) {
  const [amount, setAmount] = useState(String(reserve))
  const [error, setError] = useState<string>()
  const save = async () => {
    setError(undefined)
    const n = Number(amount)
    if (!Number.isInteger(n) || n < 0) {
      setError('Enter a whole-dollar amount of 0 or more.')
      return
    }
    try {
      await demoApi.setReserve(exposureId, n)
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not set the reserve')
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 text-[12px]">
      <span className="min-w-40">
        {type}: <span className="tabular-nums">${reserve.toLocaleString('en-US')}</span>
      </span>
      <input
        value={amount}
        onChange={(e) => setAmount(e.target.value)}
        inputMode="numeric"
        aria-label={`Reserve for ${type}`}
        className="w-28 rounded-sm border border-slate-300 px-2 py-1 tabular-nums"
      />
      <button
        onClick={save}
        className="rounded-sm border border-slate-300 px-2 py-1 font-medium hover:bg-slate-50"
      >
        Set reserve
      </button>
      {error && <span className="text-red-700">{error}</span>}
    </div>
  )
}

function PartiesTab({ claim }: { claim: DemoClaim }) {
  return (
    <Card title="Parties">
      <table className="w-full text-left text-[12px]">
        <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
          <tr>
            <th className="px-2 py-1 font-medium">Role</th>
            <th className="px-2 py-1 font-medium">Name</th>
            <th className="px-2 py-1 font-medium">Phone</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200">
          {claim.parties.map((p, i) => (
            <tr key={i}>
              <td className="px-2 py-1">{p.role}</td>
              <td className="px-2 py-1">{p.name ?? '—'}</td>
              <td className="px-2 py-1">{p.phone ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function PolicyTab({ claim }: { claim: DemoClaim }) {
  const p = claim.policy
  if (!p) return <Card title="Policy">{claim.policy_number} was not found.</Card>
  return (
    <Card title="Policy">
      <dl className="grid grid-cols-2 gap-1 text-[12px]">
        <dt className="text-slate-500">Policy</dt>
        <dd>{p.policy_no}</dd>
        <dt className="text-slate-500">Holder</dt>
        <dd>{p.holder}</dd>
        <dt className="text-slate-500">Vehicle</dt>
        <dd>{p.vehicle}</dd>
        <dt className="text-slate-500">State</dt>
        <dd>{p.state}</dd>
        <dt className="text-slate-500">Expires</dt>
        <dd>{p.expires}</dd>
      </dl>
    </Card>
  )
}

function FinancialsTab({ claim, reload }: { claim: DemoClaim; reload: () => void }) {
  return (
    <>
      <Card title="Totals">
        <FinancialTotals claim={claim} />
      </Card>
      <Card title="Reserves by exposure">
        <div className="space-y-2">
          {claim.exposures.map((e) => (
            <ReserveRow key={e.id} exposureId={e.id} type={e.type} reserve={e.reserve} reload={reload} />
          ))}
        </div>
      </Card>
    </>
  )
}

function WorkplanTab({ claim, reload }: { claim: DemoClaim; reload: () => void }) {
  const done = async (id: string) => {
    await demoApi.completeActivity(id)
    reload()
  }
  return (
    <Card title="Workplan">
      <table className="w-full text-left text-[12px]">
        <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
          <tr>
            <th className="px-2 py-1 font-medium">Subject</th>
            <th className="px-2 py-1 font-medium">Due</th>
            <th className="px-2 py-1 font-medium">Priority</th>
            <th className="px-2 py-1 font-medium">Source</th>
            <th className="px-2 py-1 font-medium">Status</th>
            <th className="px-2 py-1 font-medium"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200">
          {claim.activities.map((a) => (
            <tr key={a.id}>
              <td className="px-2 py-1">{a.subject}</td>
              <td className="px-2 py-1 tabular-nums">{a.due_date}</td>
              <td className="px-2 py-1">{a.priority}</td>
              <td className="px-2 py-1">{a.source}</td>
              <td className="px-2 py-1">{a.status}</td>
              <td className="px-2 py-1">
                {a.status === 'Open' && (
                  <button
                    onClick={() => done(a.id)}
                    className="rounded-sm border border-slate-300 px-2 py-0.5 hover:bg-slate-50"
                  >
                    Mark done
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

function NotesTab({ claim, reload }: { claim: DemoClaim; reload: () => void }) {
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [error, setError] = useState<string>()
  const add = async () => {
    setError(undefined)
    try {
      await demoApi.addNote(claim.claim_number, subject, body)
      setSubject('')
      setBody('')
      reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not add the note')
    }
  }
  return (
    <>
      <Card title="Add note">
        {error && <p className="mb-2 text-red-700">{error}</p>}
        <div className="space-y-2">
          <input
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Subject"
            className="block w-full rounded-sm border border-slate-300 px-2 py-1"
          />
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder="Body"
            rows={3}
            className="block w-full rounded-sm border border-slate-300 px-2 py-1"
          />
          <button
            onClick={add}
            disabled={!subject || !body}
            className="rounded-sm bg-slate-700 px-3 py-1 text-[12px] font-medium text-white hover:bg-slate-600 disabled:opacity-50"
          >
            Add note
          </button>
        </div>
      </Card>
      <Card title={`Notes (${claim.notes.length})`}>
        <ul className="space-y-2">
          {claim.notes.map((n) => (
            <li key={n.id} className="border-b border-slate-100 pb-2 text-[12px] last:border-0">
              <p className="font-medium">
                {n.subject}{' '}
                <span className="font-normal text-slate-500">
                  · {n.author} · {n.created_at}
                </span>
              </p>
              <p className="whitespace-pre-wrap">{n.body}</p>
            </li>
          ))}
        </ul>
      </Card>
    </>
  )
}

function DocumentsTab({ claim }: { claim: DemoClaim }) {
  return (
    <Card title={`Documents (${claim.documents.length})`}>
      <table className="w-full text-left text-[12px]">
        <thead className="bg-slate-100 text-[11px] uppercase text-slate-500">
          <tr>
            <th className="px-2 py-1 font-medium">Name</th>
            <th className="px-2 py-1 font-medium">Type</th>
            <th className="px-2 py-1 font-medium">Source</th>
            <th className="px-2 py-1 font-medium"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-200">
          {claim.documents.map((d) => (
            <tr key={d.id}>
              <td className="px-2 py-1">{d.name}</td>
              <td className="px-2 py-1">{d.document_type}</td>
              <td className="px-2 py-1">{d.source}</td>
              <td className="px-2 py-1">
                <a href={d.url} target="_blank" rel="noreferrer" className="underline">
                  Open
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  )
}

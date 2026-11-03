import type { PolicyOption } from './api'
import { ApiError } from './api'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (res.status === 401 && path.startsWith('/api/demo-insurer')) {
    const next = encodeURIComponent(window.location.pathname)
    window.location.assign(`/login?next=${next}`)
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`
    try {
      message = (await res.json()).detail ?? message
    } catch {
      // keep the generic message
    }
    throw new ApiError(message, res.status)
  }
  return res.json()
}

function json(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

export interface DemoDesktopActivity {
  id: string
  claim_number: string
  subject: string
  priority: string
  due_date: string
  status: string
  source: string
}

export interface DemoDesktopClaim {
  claim_number: string
  insured: string | null
  loss_date: string
  status: string
  service_status: string | null
}

export interface DemoDesktop {
  activities: DemoDesktopActivity[]
  claims: DemoDesktopClaim[]
}

export interface DemoParty {
  role: string
  name: string | null
  phone?: string | null
}

export interface DemoExposure {
  id: string
  type: string
  coverage: string
  claimant: string | null
  reserve: number
  status: string
}

export interface DemoService {
  id: string
  claim_id: string
  type: string
  vendor: string
  status: string
  vendor_ref: string | null
  capture_url: string | null
  vendor_base_url: string | null
  requested_at: string
  completed_at: string | null
}

export interface DemoActivity {
  id: string
  subject: string
  description: string | null
  priority: string
  due_date: string
  status: string
  source: string
  created_at: string
  completed_at: string | null
}

export interface DemoNote {
  id: string
  subject: string
  body: string
  author: string
  created_at: string
}

export interface DemoDocument {
  id: string
  name: string
  document_type: string
  url: string
  sha256: string | null
  source: string
  created_at: string
}

export interface DemoFlag {
  id: string
  level: string
  reason: string
  evidence: string[]
  source: string
  created_at: string
}

export interface DemoClaim {
  id: string
  claim_number: string
  policy_number: string
  insured_name: string | null
  vehicle: { vehicle: string; state?: string }
  loss_date: string
  loss_cause: string
  description: string
  claimant_phone: string | null
  status: string
  created_at: string
  policy: PolicyOption | null
  parties: DemoParty[]
  exposures: DemoExposure[]
  financials: { reserves: number; paid: number; recovered: number }
  services: DemoService[]
  activities: DemoActivity[]
  notes: DemoNote[]
  documents: DemoDocument[]
  flags: DemoFlag[]
}

export const demoApi = {
  desktop: () => request<DemoDesktop>('/api/demo-insurer/desktop'),
  policies: () => request<PolicyOption[]>('/api/demo-insurer/policies'),
  createClaim: (body: {
    policy_number: string
    loss_date: string
    loss_cause: string
    description: string
    claimant_phone: string | null
  }) => request<DemoClaim>('/api/demo-insurer/claims', json(body)),
  claim: (claimNumber: string) => request<DemoClaim>(`/api/demo-insurer/claims/${claimNumber}`),
  requestCapture: (claimNumber: string) =>
    request<DemoClaim>(`/api/demo-insurer/claims/${claimNumber}/services/verified-capture`, json({})),
  addNote: (claimNumber: string, subject: string, body: string) =>
    request<DemoClaim>(`/api/demo-insurer/claims/${claimNumber}/notes`, json({ subject, body })),
  completeActivity: (activityId: string) =>
    request<DemoClaim>(`/api/demo-insurer/activities/${activityId}/complete`, json({})),
  setReserve: (exposureId: string, amount: number) =>
    request<DemoClaim>(`/api/demo-insurer/exposures/${exposureId}/reserve`, json({ amount })),
}

export type SlotKind = 'photo' | 'video' | 'signature'

export interface Field {
  id: string
  type: 'text' | 'tel' | 'email' | 'date' | 'time' | 'number' | 'vin' | 'textarea' | 'choice' | 'multiselect' | 'checkbox'
  label: string
  hint?: string
  options?: string[]
  required: boolean
  value: Answer | undefined
  error: string | null
  note: string | null
  prefilled?: boolean
  /** T45: the one-topic-per-screen group this field belongs to; a missing key is its own screen. */
  screen?: string
  /** T45: the widget rendering this field, e.g. "car_parts" for damaged_areas. */
  widget?: string
  /** T59: the option that opens a free-text answer, stored as `<id>_other`. Absent on closed questions. */
  other?: string
  /** T59: the claimant's own words for the escape option, if any. */
  other_text?: string | null
}

export interface Slot {
  id: string
  kind: SlotKind
  title: string
  instructions: string
  required: boolean
  multiple?: boolean
  max_items?: number
  max_seconds?: number
  count: number
  skippable: boolean
  skipped: { reason: string; note?: string } | null
}

export interface ClaimantPolicy {
  policy_no: string
  verified: boolean
  state: string | null
  holder?: string
  vehicle?: string
  plate?: string
  vin?: string
  coverages?: Record<string, { deductible?: number; limits?: string }>
}

export interface Section {
  id: string
  number: number
  title: string
  intro: string | null
  fields: Field[]
  slots: Slot[]
  missing: string[]
  complete: boolean
  policy?: ClaimantPolicy | null
  notice?: string
}

export interface Layout {
  loss_type: string | null
  loss_types: { id: string; label: string; description: string; group: string }[]
  policy: ClaimantPolicy | null
  skip_reasons: string[]
  screen_titles: Record<string, string>
  screen_order: string[]
  sections: Section[]
  complete: boolean
  missing: string[]
  quality?: { min_sharpness: number; min_brightness: number; max_brightness: number }
}

export type SlotSkips = Record<string, { reason: string; note?: string }>
export type Answer = string | string[] | boolean | SlotSkips
export type Answers = Record<string, Answer>

/** T60: the loss-type suggestion for the claimant's own words; all null when unsure. */
export interface ClassifySuggestion {
  loss_type: string | null
  label: string | null
  confidence: string | null
  reason: string | null
}

export interface Item {
  id: string
  step_id: string
  kind: SlotKind
  mime: string
  size: number
  sha256: string
  received_at: string
  superseded: boolean
  client_meta: Record<string, unknown>
  removed_at?: string | null
  removed_reason?: string | null
  flags?: string[]
  static_source?: string | null
  slow_upload?: boolean
  nonce_to_upload_latency_seconds?: number | null
  server_facts?: Record<string, unknown>
  facts?: Record<string, unknown>
  liveness?: { status?: string; reason?: string | null } | null
}

export interface PhotoCheck {
  item_id: string
  slot_id: string
  result: 'pending' | 'pass' | 'retake' | 'flagged' | 'unchecked'
  hint: string | null
  shots_left: number
}

export interface SessionState {
  claim_id: string
  insurer_claim_number: string | null
  brand: { name: string; color: string }
  status: ClaimStatus
  answers: Answers
  layout: Layout
  items: Item[]
  photo_checks: PhotoCheck[]
  /** After a Send back (round 2): the only slots the claimant may retake, and those not retaken yet. */
  requested_slots?: string[]
  retakes_left?: string[]
}

export type ClaimStatus = string

export interface ClaimSummary {
  id: string
  status: ClaimStatus
  created_at: string
  submitted_at: string | null
  item_count: number
  policy_number: string
  policy_verified: boolean
  holder: string | null
  insurer_claim_number: string | null
}

export interface PolicyOption {
  policy_no: string
  holder: string
  vehicle: string
  state: string
  expires: string
}

export interface PolicySnapshot {
  id: string
  policy_no: string
  verified: boolean
  sha256: string
  /** T53: where the frozen policy came from; absent on older rows. */
  source?: 'on_file' | 'insurer' | 'trial' | null
  data: {
    state: string
    effective: string
    expires: string
    holder: { name: string; phone: string; email: string }
    /** T53: who entered the car in the console; trial policies only. */
    entered_by?: string | null
    drivers: { name: string; licence_no: string; licence_state: string }[]
    vehicles: { vin: string; plate: string; year: number; make: string; model: string }[]
    coverages: Record<string, { deductible?: number; limits?: string }>
  } | null
}

export interface ClaimEvent {
  id: string
  seq: number
  at: string
  actor: 'customer' | 'user' | 'system'
  type: string
  from_status: string | null
  to_status: string | null
  data: Record<string, unknown>
  hash: string
}

export interface Damage {
  key: string
  item_id: string
  slot_id: string
  number: number
  area: string
  area_read?: string
  damage_type: string
  severity: string
  age: 'new' | 'old' | 'unsure'
  box: [number, number, number, number] | null
  observed: string
  outline?: DamageOutline | null
}

// Track 6: SAM 3.1's outline of a damage Muse Spark boxed. null or "not_run": Muse's box only.
export interface DamageOutline {
  status: 'done' | 'not_run' | 'failed'
  error: string | null
  concept: string | null
  sam_agrees: 'yes' | 'no' | 'not_run'
  regions: { box: [number, number, number, number]; mask: 'decoded' | 'not_decoded'; overlay: string | null }[]
  area: { photo_pct: number | null; panel_pct: number | null; basis: 'mask' | 'box estimate'; panel: string | null } | null
}

export interface DamagePhoto {
  item_id: string
  slot_id: string
  status: 'pending' | 'done' | 'failed'
  error: string | null
  is_vehicle: boolean | null
  unsure: boolean
  damages: Damage[]
  dropped: number
}

export interface DamageView {
  status: 'none' | 'pending' | 'done' | 'partial' | 'failed'
  photos: DamagePhoto[]
  damages: Damage[]
  areas: string[]
  old_damage: boolean
}

export interface Check {
  check_id: string
  result: 'pass' | 'unclear' | 'fail' | 'value'
  reason_code: string | null
  reason: string
  value: unknown
  inputs: Record<string, unknown>
  source: string
  created_at: string
}

export interface EstimateRef {
  item_id: string
  slot_id: string
  number: number
  damage_type: string
  severity: string
}

export interface PricedItem extends EstimateRef {
  low: number
  high: number
}

export interface RoughEstimate {
  status: 'complete' | 'incomplete' | 'none'
  priced: PricedItem[]
  unpriced: EstimateRef[]
  excluded_old: EstimateRef[]
  full: [number, number] | null
  likely: [number, number] | null
  midpoint: number | null
  basis: string
  version: string
}

export interface ProposalFlag {
  key: string
  source: string
  level: 'conflict' | 'review' | 'note'
  reason_code: string | null
  reason: string
  evidence: { item_id: string; slot_id: string; number?: number }[]
  /** T52: the review step this finding belongs to (coverage, liability, damage, estimate). */
  step?: string | null
  /** T52: form field ids the finding is about, used to highlight answers in the loss notice. */
  fields?: string[]
}

export interface ProposalEvidence {
  band: 'low' | 'moderate' | 'strong'
  contributions: { cause: string; label: string; level: 'conflict' | 'review'; model_only: boolean }[]
  missing: { cause: string; label: string }[]
  note: string
}

export interface ProposalReady {
  status: 'ready'
  version: string
  lane: 'siu_referral' | 'adjuster_review' | 'fast_track' | 'standard'
  label: string
  rule: string
  headline: string
  lane_rule?: 'cost' | 'count'
  evidence?: ProposalEvidence
  reasons: string[]
  flags: ProposalFlag[]
  notes: ProposalFlag[]
  estimate: RoughEstimate
  deductible: number | null
  suggested_reserve: number | null
  next_step: string
}

export type Proposal = { status: 'pending' } | ProposalReady

export interface ReportOutboxMessage {
  id: string
  event: string
  created_at: string
  delivered_at: string | null
  attempts: number
  last_error: string | null
  body: unknown
}

export interface ClaimReport {
  url: string
  sha256: string
  version: string
  created_at: string
  expires_at: string
  outbox: ReportOutboxMessage[]
  reviewed_sha256: string | null
  reviewed_at: string | null
}

export interface AiSentence {
  text: string
  refs: string[]
}

export interface AiSummary {
  status: 'verified' | 'rejected' | 'failed'
  sentences: AiSentence[]
  reasons: string[]
  /** What code checked, from ai_summary.CAPTION on the server. */
  caption?: string
}

export interface ReviewEntry {
  verdict: string
  reason: string | null
  by: string
  at: string
}

export interface ReviewState {
  flags: Record<string, ReviewEntry>
  damages: Record<string, ReviewEntry>
  notes: { text: string; by: string; at: string }[]
}

export type DeliveryMode = 'on_ready' | 'on_decision'

export interface RemovalReceipt {
  claim_id: string
  removed: { file: string; sha256: string | null }[]
  removed_at: string | null
  reason: 'retention' | 'request' | null
  by: string | null
}

export interface OrgSettings {
  delivery_mode: DeliveryMode
  brand_color?: string
  retention_days: number
  updated_by: string | null
  updated_at: string | null
}

export interface OrgSettingsUpdate {
  delivery_mode?: DeliveryMode
  brand_color?: string
  retention_days?: number
}

export type ReviewInput =
  | { kind: 'flag'; key: string; verdict: 'confirmed' | 'false_alarm'; reason?: string }
  | { kind: 'damage'; key: string; verdict: 'excluded' | 'included'; reason?: string }
  | { kind: 'note'; text: string }

export interface DecisionView {
  action: 'approve' | 'edit_approve' | 'send_back' | 'refer_siu' | 'field_inspection'
  label: string
  lane_before: string | null
  lane_after: string | null
  reserve_before: number | null
  reserve_after: number | null
  reason: string | null
  slots: string[]
  decided_by: string
  decided_at: string
  version: string
}

export interface CycleView {
  link_to_submit: number | null
  submit_to_report: number | null
  report_to_decision: number | null
  link_to_decision: number | null
}

export interface QueueClaim extends ClaimSummary {
  lane: string | null
  headline: string | null
  flags: number
  vehicle: string | null
  /** T52: the type label, e.g. "Glass", null before the claimant picks. */
  claim_type?: string | null
  /** T52: "YYYY-MM-DD", null before a report exists. */
  review_due?: string | null
  /** T52: true when review_due is before today and the claim has no decision. */
  overdue?: boolean
  /** T52: the earliest open diary's due_on, else null. */
  diary_due?: string | null
  /** T52: that diary's reason, else null. */
  diary_reason?: string | null
  /** T53: true when the claim was opened from a trial policy entered in the console. */
  trial?: boolean
}

export interface Diary {
  id: string
  due_on: string
  reason: string
  created_by: string | null
  created_at: string
  done_at: string | null
  done_by: string | null
}

export interface Contact {
  name: string | null
  phone: string | null
  email: string | null
}

/** T57: the model actually reading photos, from `GET /api/contact` (see `PROVIDERS` in meta_client.py). */
export interface ContactModel {
  provider: 'meta' | 'fireworks'
  model: string
  host: string
  retention: string
}

/** T57: the public contact address plus the model actually reading photos. */
export interface PublicContact {
  email: string | null
  model: ContactModel | null
}

export interface CoverageView {
  type: 'collision' | 'comprehensive' | null
  deductible: number | null
  lienholder: string | null
}

export interface QueueGroup {
  key: string
  label: string
  count: number
  claims: QueueClaim[]
}

export interface QueueView {
  groups: QueueGroup[]
  averages: Record<string, { mean_s: number | null; n: number }>
}

export interface SystemHealth {
  window_hours: number
  generated_at: string
  analyses_failed: number
  photo_checks_failed: number
  outbox: { dead: number; retrying: number; delivered: number }
  model_calls: { total: number; errors: number; error_rate: number | null; budget_refused: number; cost_usd: number }
  last_sweep: { at: string; result: Record<string, number>; error: string | null } | null
  /** T53: the model serving predictions, from `health.model`. */
  model: { provider: string; model: string; host: string; retention: string }
  /** T57: accuracy measured by the E1 test set (counts and 95% Wilson intervals), or null. */
  accuracy: ModelAccuracy | null
  claims: string[]
}

export interface ModelAccuracy {
  measured_on: string
  model: string
  host: string
  provider: string
  set: { photos: number; claims: number; about: string }
  rows: { id: string; label: string; hits: number; n: number; ci: [number, number] | null; lower_is_better: boolean }[]
  cost: { calls: number; usd: number; p50_s: number; p95_s: number }
}

export interface FeedbackRow {
  reason_code: string
  confirmed: number
  false_alarm: number
  total: number
  confirm_rate: number
  ci: [number, number] | null
}

export interface FeedbackView {
  rows: FeedbackRow[]
  reviews: number
}

export interface DecisionInput {
  action: string
  reason?: string
  lane?: string
  suggested_reserve?: number
  slots?: string[]
}

/** An insurer API key (T48): the full key is shown once, at creation. */
export interface ApiKey {
  id: string
  name: string
  mode: 'live' | 'test'
  prefix: string
  created_at: string
  created_by: string | null
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
  status: 'active' | 'expiring' | 'expired' | 'revoked'
}

export interface NewApiKey extends ApiKey {
  key: string
}

export interface WebhookSecretInfo {
  last4: string
  rotated_at: string | null
}

export interface TestWebhookResult {
  ok: boolean
  status_code?: number
  ms?: number
  error?: string
}

export interface DevelopersInfo {
  events: Record<string, unknown>
  verify: { python: string; node: string }
  limits: {
    api_per_minute: number
    retries: number
    first_retry_seconds: number
    max_retry_wait_seconds: number
  }
}

/** The signed-in adjuster or supervisor. */
export interface Me {
  id: string
  email: string
  name: string
  role: 'adjuster' | 'supervisor'
  must_change_password: boolean
}

export interface UserRow {
  id: string
  email: string
  name: string
  role: 'adjuster' | 'supervisor'
  active: boolean
  must_change_password: boolean
  created_at: string
}

export interface NewUser {
  id: string
  email: string
  name: string
  role: 'adjuster' | 'supervisor'
  active: boolean
  must_change_password: boolean
  created_at: string
  one_time_password: string
}

export interface ClaimType {
  id: string
  label: string
}

export interface ClaimDetail {
  id: string
  token: string
  report: ClaimReport | null
  removal: RemovalReceipt | null
  decision: DecisionView | null
  cycle: CycleView
  insurer_claim_number: string | null
  /** T48: a test key made this claim; it lives in the Test lane, never counted. */
  test_mode: boolean
  callback_url: string | null
  claim_type: ClaimType | null
  status: ClaimStatus

  created_at: string
  expires_at: string
  submitted_at: string | null
  answers: Answers
  /** Claimant-reported items to confirm with the issuer, e.g. the police report. */
  to_verify?: string[]
  layout: Layout
  checks: Check[]
  consistency: { status: 'none' | 'pending' | 'done' | 'failed' }
  damage: DamageView
  proposal: Proposal | null
  human_review_required: boolean
  sessions: { code: string; started_at: string; expires_at: string; superseded: boolean }[]
  seal: { sealed_at: string; key_id: string; manifest_sha256: string } | null
  items: Item[]
  policy: PolicySnapshot
  events: ClaimEvent[]
  decisions: Record<'liability' | 'coverage_eligibility' | 'damage_severity' | 'payment_approval', string>
  reason_codes: string[]
  missing_information: string[]
  summary: string
  ai_summary: AiSummary | null
  review: ReviewState
  delivery_mode: DeliveryMode
  /** T52: claimant contact when given, else the policy holder; missing values are null. */
  contact?: Contact | null
  /** T52: coverage type, deductible and lienholder. */
  coverage?: CoverageView | null
  /** T52: "YYYY-MM-DD", null before a report exists. */
  review_due?: string | null
  /** T52: open first by due date, then done ones newest first. */
  diaries?: Diary[]
}

export interface CaptureSession {
  session_id: string
  code: string
  expires_at: string
}

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export interface UploadProgress {
  loaded: number
  total: number | null
}

export interface UploadOptions {
  onProgress?: (p: UploadProgress) => void
  signal?: AbortSignal
  frameBefore?: Blob | null
  frameAfter?: Blob | null
}

/** True when the capture session behind this error is gone (superseded or expired). */
export function isSessionDead(e: unknown): boolean {
  return e instanceof ApiError && e.status === 403 && /not active|has expired/i.test(e.message)
}

const UPLOAD_INACTIVITY_MS = 15_000
const UPLOAD_RESPONSE_MS = 8_000 // after the last byte is sent, plus 1 s per 5 MB
const UPLOAD_OVERALL_MS = 120_000

function parseErrorDetail(text: string, status: number): string {
  try {
    return (JSON.parse(text) as { detail?: string }).detail ?? `Request failed (${status})`
  } catch {
    return `Request failed (${status})`
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init)
  if (res.status === 401 && path.startsWith('/api/admin') && path !== '/api/admin/login') {
    // Session missing or expired: send the adjuster to the login page and come back.
    const next = encodeURIComponent(window.location.pathname)
    window.location.assign(`/login?next=${next}`)
  }
  if (
    res.status === 403 &&
    path.startsWith('/api/admin') &&
    !['/api/admin/me', '/api/admin/me/password', '/api/admin/logout'].includes(path) &&
    window.location.pathname !== '/change-password'
  ) {
    try {
      const body = (await res.clone().json()) as { detail?: string }
      if (body.detail === 'Choose a new password first') {
        const next = encodeURIComponent(window.location.pathname)
        window.location.assign(`/change-password?next=${next}`)
      }
    } catch {
      // keep the generic error below
    }
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

/** T53/T56: the console trial form payload; the server builds and freezes the policy. */
export interface TrialClaimBody {
  policy_number?: string
  holder_name: string
  state: string
  vehicle: { year: number | string; make: string; model: string; plate: string; vin: string }
  collision_deductible?: number | string
  comprehensive_deductible?: number | string
}

export const api = {
  login: (email: string, password: string) =>
    request<{ user: Me }>('/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/admin/logout', { method: 'POST' }),
  me: () => request<Me>('/api/admin/me'),
  changePassword: (current: string, next: string) =>
    request<{ user: Me }>('/api/admin/me/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current, new: next }),
    }),
  listUsers: () => request<UserRow[]>('/api/admin/users'),
  createUser: (body: { email: string; name: string; role: 'adjuster' | 'supervisor' }) =>
    request<NewUser>('/api/admin/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  updateUser: (id: string, body: { name?: string; role?: 'adjuster' | 'supervisor'; active?: boolean }) =>
    request<UserRow>(`/api/admin/users/${id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  resetUserPassword: (id: string) =>
    request<{ one_time_password: string }>(`/api/admin/users/${id}/reset-password`, {
      method: 'POST',
    }),
  listPolicies: () => request<PolicyOption[]>('/api/admin/policies'),
  createClaim: (policyNumber: string) =>
    request<{ id: string; token: string }>('/api/admin/claims', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ policy_number: policyNumber }),
    }),
  createTrialClaim: (body: TrialClaimBody) =>
    request<{ id: string; token: string; policy_number: string }>('/api/admin/claims', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ trial: body }),
    }),
  listClaims: () => request<ClaimSummary[]>('/api/admin/claims'),
  claim: (id: string) => request<ClaimDetail>(`/api/admin/claims/${id}`),
  deliver: (id: string) =>
    request<{ ok: boolean }>(`/api/admin/claims/${id}/deliver`, { method: 'POST' }),
  decide: (id: string, body: DecisionInput) =>
    request<ClaimDetail>(`/api/admin/claims/${id}/decision`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  queue: () => request<QueueView>('/api/admin/queue'),
  health: () => request<SystemHealth>('/api/admin/health'),
  feedback: () => request<FeedbackView>('/api/admin/feedback'),
  fileUrl: (itemId: string) => `/api/admin/items/${itemId}/file`,
  review: (id: string, body: ReviewInput) =>
    request<ClaimDetail>(`/api/admin/claims/${id}/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  getSettings: () => request<OrgSettings>('/api/admin/settings'),
  updateSettings: (body: OrgSettingsUpdate) =>
    request<OrgSettings>('/api/admin/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  listApiKeys: () => request<ApiKey[]>('/api/admin/api-keys'),
  createApiKey: (body: { name: string; mode: 'live' | 'test' }) =>
    request<NewApiKey>('/api/admin/api-keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  revokeApiKey: (id: string) =>
    request<ApiKey>(`/api/admin/api-keys/${id}/revoke`, { method: 'POST' }),
  rotateApiKey: (id: string, oldKeyExpiresIn?: 'now' | '1h' | '24h') =>
    request<NewApiKey>(`/api/admin/api-keys/${id}/rotate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(oldKeyExpiresIn ? { old_key_expires_in: oldKeyExpiresIn } : {}),
    }),
  getWebhookSecret: () => request<WebhookSecretInfo>('/api/admin/webhook-secret'),
  rotateWebhookSecret: () =>
    request<{ secret: string }>('/api/admin/webhook-secret/rotate', { method: 'POST' }),
  sendTestWebhook: (url: string) =>
    request<TestWebhookResult>('/api/admin/webhooks/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    }),
  developers: () => request<DevelopersInfo>('/api/developers'),
  /** T54: the public contact address. Plain fetch: public, never redirects to login. T57: also the model actually reading photos. */
  contact: (): Promise<PublicContact> =>
    fetch('/api/contact').then(async (res) => {
      if (!res.ok) return { email: null, model: null }
      const body = (await res.json()) as { email?: unknown; model?: unknown }
      const m = body.model as Record<string, unknown> | null | undefined
      const model: ContactModel | null =
        m != null &&
        (m.provider === 'meta' || m.provider === 'fireworks') &&
        typeof m.model === 'string' &&
        typeof m.host === 'string' &&
        typeof m.retention === 'string'
          ? { provider: m.provider, model: m.model, host: m.host, retention: m.retention }
          : null
      return { email: typeof body.email === 'string' ? body.email : null, model }
    }),
  deleteMedia: (id: string, reason: string) =>
    request<{ ok: boolean }>(`/api/admin/claims/${id}/delete-media`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }),
  createDiary: (id: string, body: { due_on: string; reason: string }) =>
    request<Diary>(`/api/admin/claims/${id}/diaries`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  completeDiary: (diaryId: string) =>
    request<Diary>(`/api/admin/diaries/${diaryId}/done`, { method: 'POST' }),

  session: (token: string) => request<SessionState>(`/api/c/${token}`),
  claimantFileUrl: (token: string, itemId: string) => `/api/c/${token}/items/${itemId}/file`,
  saveDraft: (token: string, answers: Answers) =>
    request<SessionState>(`/api/c/${token}/draft`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    }),
  classify: (token: string, text: string) =>
    request<ClassifySuggestion>(`/api/c/${token}/classify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    }),
  submit: (token: string, answers: Answers) =>
    request<{ claim_id: string; submitted_at: string }>(`/api/c/${token}/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    }),
  startSession: (token: string) => request<CaptureSession>(`/api/c/${token}/session`, { method: 'POST' }),
  nonce: (token: string, sessionId: string, slotId: string) =>
    request<{ nonce: string; expires_at: string }>(`/api/c/${token}/nonce`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, slot_id: slotId }),
    }),
  removeItem: (token: string, sessionId: string, itemId: string) =>
    request<{ ok: boolean }>(`/api/c/${token}/items/${itemId}?session_id=${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),

  async uploadItem(
    token: string,
    sessionId: string,
    slotId: string,
    nonce: string,
    blob: Blob,
    meta: Record<string, unknown>,
    options?: UploadOptions,
  ): Promise<Item> {
    // fetch has no upload progress and no timeout on iOS Safari, so uploads go
    // through XHR: progress keeps the screen alive, an inactivity timer aborts
    // a stalled radio, and an overall cap stops it hanging forever.
    const sendOnce = () =>
      new Promise<Item>((resolve, reject) => {
        const ext = blob.type.includes('mp4') ? 'mp4' : blob.type.includes('webm') ? 'webm' : 'jpg'
        const body = new FormData()
        body.append('step_id', slotId)
        body.append('session_id', sessionId)
        body.append('nonce', nonce)
        body.append('client_meta', JSON.stringify(meta))
        body.append('file', blob, `${slotId}.${ext}`)
        if (options?.frameBefore) body.append('frame_before', options.frameBefore, 'frame_before.jpg')
        if (options?.frameAfter) body.append('frame_after', options.frameAfter, 'frame_after.jpg')
        const xhr = new XMLHttpRequest()
        const signal = options?.signal
        let settled = false
        let abortReason: 'idle' | 'overall' | 'user' | null = null
        let idleTimer = 0
        let overallTimer = 0
        const onSignalAbort = () => {
          abortReason = 'user'
          xhr.abort()
        }
        const cleanup = () => {
          window.clearTimeout(idleTimer)
          window.clearTimeout(overallTimer)
          signal?.removeEventListener('abort', onSignalAbort)
        }
        const fail = (message: string, status: number) => {
          if (settled) return
          settled = true
          cleanup()
          reject(new ApiError(message, status))
        }
        const rearmIdle = () => {
          window.clearTimeout(idleTimer)
          idleTimer = window.setTimeout(() => {
            abortReason = 'idle'
            xhr.abort()
          }, UPLOAD_INACTIVITY_MS)
        }
        xhr.upload.onprogress = (e) => {
          rearmIdle()
          options?.onProgress?.({ loaded: e.loaded, total: e.lengthComputable ? e.total : null })
        }
        // iOS can push the whole body into a connection the network already dropped:
        // progress reaches 100% and no answer ever comes. The server answers within a
        // couple of seconds once it has the file, so a long silence here means retry.
        xhr.upload.onload = () => {
          window.clearTimeout(idleTimer)
          idleTimer = window.setTimeout(() => {
            abortReason = 'idle'
            xhr.abort()
          }, UPLOAD_RESPONSE_MS + (blob.size / 5_000_000) * 1000)
        }
        xhr.onload = () => {
          if (settled) return
          if (xhr.status >= 200 && xhr.status < 300) {
            try {
              const item = JSON.parse(xhr.responseText) as Item
              settled = true
              cleanup()
              resolve(item)
            } catch {
              fail('Upload failed (bad response)', xhr.status)
            }
            return
          }
          fail(parseErrorDetail(xhr.responseText, xhr.status), xhr.status)
        }
        xhr.onerror = () => fail('Network error. Check your connection and try again.', 0)
        xhr.onabort = () => {
          if (abortReason === 'user') fail('Upload cancelled.', 0)
          else if (abortReason === 'overall') fail('Upload took too long. Check your connection and try again.', 0)
          else fail('Upload stalled. Check your connection and try again.', 0)
        }
        signal?.addEventListener('abort', onSignalAbort)
        xhr.open('POST', `/api/c/${token}/items`)
        options?.onProgress?.({ loaded: 0, total: blob.size })
        rearmIdle()
        overallTimer = window.setTimeout(() => {
          abortReason = 'overall'
          xhr.abort()
        }, UPLOAD_OVERALL_MS)
        xhr.send(body)
      })
    // Mobile networks drop requests. Retrying with the same nonce and the same
    // blob is safe: the server returns the stored item if the first attempt
    // already arrived. HTTP 4xx answers are real refusals, so show their message.
    let lastError: unknown
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await sendOnce()
      } catch (err) {
        if (options?.signal?.aborted) throw err
        if (err instanceof ApiError && err.status >= 400 && err.status < 500) throw err
        lastError = err
        if (attempt < 2) await new Promise((r) => setTimeout(r, 500 * (attempt + 1)))
      }
    }
    throw lastError
  },
}

export interface VerifyResult {
  valid: boolean
  claim_id: string | null
  sealed_at: string | null
  items?: number
  removed?: number
  removed_at?: string | null
  removed_reason?: 'retention' | 'request' | null
  problems: string[]
}

export function verifyPackage(file: File) {
  const body = new FormData()
  body.append('file', file)
  return request<VerifyResult>('/api/verify', { method: 'POST', body })
}

export function packageUrl(claimId: string) {
  return `/api/admin/claims/${claimId}/package`
}

export function captureLink(token: string) {
  return `${window.location.origin}/c/${token}`
}

export function formatBytes(n: number) {
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function formatTime(iso: string | null) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export function formatAge(iso: string | null) {
  if (!iso) return '—'
  const ms = Date.now() - new Date(iso).getTime()
  if (!Number.isFinite(ms) || ms < 0) return 'just now'
  const s = Math.floor(ms / 1000)
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} h ago`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d} d ago`
  return formatTime(iso)
}

export function reportDraftUrl(secretUrl: string) {
  return secretUrl.includes('?') ? `${secretUrl}&version=draft` : `${secretUrl}?version=draft`
}

/** T52: "YYYY-MM-DD" -> "Nov 4". Falls back to the raw value when unparseable. */
export function formatDueDate(due: string | null | undefined) {
  if (!due) return '—'
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due)
  if (!m) return due
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  const month = Number(m[2])
  const day = Number(m[3])
  if (month < 1 || month > 12) return due
  return `${months[month - 1]} ${day}`
}

/** T52: "Maria Lopez" -> "ML". */
export function initialsOf(name: string | null | undefined) {
  if (!name) return '—'
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '—'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

/** T52: today in the browser's timezone as "YYYY-MM-DD". */
export function todayIso() {
  const d = new Date()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

export function formatDuration(totalSeconds: number | null | undefined) {
  if (totalSeconds == null) return '–'
  const s = Math.max(0, Math.round(totalSeconds))
  if (s < 60) return `${s} s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} min ${s % 60} s`
  return `${Math.floor(m / 60)} h ${m % 60} min`
}

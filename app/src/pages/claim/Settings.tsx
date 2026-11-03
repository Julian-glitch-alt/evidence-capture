import { useEffect, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import AdminLayout from '../../components/AdminLayout'
import {
  api,
  formatTime,
  type ApiKey,
  type DeliveryMode,
  type Me,
  type NewApiKey,
  type TestWebhookResult,
  type UserRow,
  type WebhookSecretInfo,
} from '../../api'

const DEFAULT_BRAND = '#1673e6'

type SettingsTab = 'team' | 'claimant' | 'reports' | 'integrations'

const SETTINGS_TABS: { key: SettingsTab; label: string }[] = [
  { key: 'team', label: 'Team' },
  { key: 'claimant', label: 'Claimant page' },
  { key: 'reports', label: 'Reports' },
  { key: 'integrations', label: 'Integrations' },
]

function parseSettingsTab(value: string | null): SettingsTab {
  return value === 'claimant' || value === 'reports' || value === 'integrations' ? value : 'team'
}

function copyText(text: string): Promise<void> {
  return navigator.clipboard.writeText(text)
}

export default function Settings() {
  const [mode, setMode] = useState<DeliveryMode>('on_ready')
  const [brand, setBrand] = useState(DEFAULT_BRAND)
  const [retention, setRetention] = useState('90')
  const [updatedBy, setUpdatedBy] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<string | null>(null)
  const [me, setMe] = useState<Me | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [users, setUsers] = useState<UserRow[]>([])
  const [newName, setNewName] = useState('')
  const [newEmail, setNewEmail] = useState('')
  const [newRole, setNewRole] = useState<'adjuster' | 'supervisor'>('adjuster')
  const [newUserOtp, setNewUserOtp] = useState<string>()
  const [resetOtps, setResetOtps] = useState<Record<string, string>>({})
  const [userError, setUserError] = useState<string>()
  const [userBusy, setUserBusy] = useState(false)
  const [copied, setCopied] = useState<string>()
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [keysError, setKeysError] = useState<string>()
  const [keysBusy, setKeysBusy] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyMode, setNewKeyMode] = useState<'live' | 'test'>('live')
  const [createdKey, setCreatedKey] = useState<NewApiKey>()
  const [rotateFor, setRotateFor] = useState<string>()
  const [rotateChoice, setRotateChoice] = useState<'now' | '1h' | '24h'>('1h')
  const [revokeFor, setRevokeFor] = useState<string>()
  const [secretInfo, setSecretInfo] = useState<WebhookSecretInfo>()
  const [secretError, setSecretError] = useState<string>()
  const [shownSecret, setShownSecret] = useState<string>()
  const [secretBusy, setSecretBusy] = useState(false)
  const [testUrl, setTestUrl] = useState('')
  const [testResult, setTestResult] = useState<TestWebhookResult>()
  const [testBusy, setTestBusy] = useState(false)
  const [testError, setTestError] = useState<string>()

  const supervisor = me?.role === 'supervisor'

  const [searchParams, setSearchParams] = useSearchParams()
  const tab = parseSettingsTab(searchParams.get('tab'))
  const setTab = (t: SettingsTab) => {
    const next = new URLSearchParams(searchParams)
    if (t === 'team') next.delete('tab')
    else next.set('tab', t)
    setSearchParams(next)
  }

  useEffect(() => {
    Promise.all([api.getSettings(), api.me()])
      .then(([s, user]) => {
        setMode(s.delivery_mode)
        setBrand(s.brand_color ?? DEFAULT_BRAND)
        setRetention(String(s.retention_days ?? 90))
        setUpdatedBy(s.updated_by)
        setUpdatedAt(s.updated_at)
        setMe(user)
        if (user.role === 'supervisor') {
          api
            .listUsers()
            .then(setUsers)
            .catch((e) => setUserError(e instanceof Error ? e.message : 'Could not load users'))
          api
            .listApiKeys()
            .then(setKeys)
            .catch((e) => setKeysError(e instanceof Error ? e.message : 'Could not load API keys'))
          api
            .getWebhookSecret()
            .then(setSecretInfo)
            .catch((e) => setSecretError(e instanceof Error ? e.message : 'Could not load the webhook secret'))
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load settings'))
      .finally(() => setLoaded(true))
  }, [])

  const save = async () => {
    setSaving(true)
    setError(undefined)
    setSaved(false)
    try {
      const days = Number(retention)
      const s = await api.updateSettings({ delivery_mode: mode, brand_color: brand, retention_days: days })
      setRetention(String(s.retention_days ?? days))
      setUpdatedBy(s.updated_by)
      setUpdatedAt(s.updated_at)
      setSaved(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save settings')
    } finally {
      setSaving(false)
    }
  }

  const addUser = async (e: FormEvent) => {
    e.preventDefault()
    setUserBusy(true)
    setUserError(undefined)
    setNewUserOtp(undefined)
    try {
      const made = await api.createUser({ email: newEmail, name: newName, role: newRole })
      setUsers((prev) => [...prev, made].sort((a, b) => a.name.localeCompare(b.name)))
      setNewUserOtp(made.one_time_password)
      setNewName('')
      setNewEmail('')
    } catch (err) {
      setUserError(err instanceof Error ? err.message : 'Could not add the user')
    } finally {
      setUserBusy(false)
    }
  }

  const changeRole = async (u: UserRow, role: 'adjuster' | 'supervisor') => {
    setUserBusy(true)
    setUserError(undefined)
    try {
      const updated = await api.updateUser(u.id, { role })
      setUsers((prev) => prev.map((x) => (x.id === u.id ? updated : x)))
    } catch (err) {
      setUserError(err instanceof Error ? err.message : 'Could not change the role')
    } finally {
      setUserBusy(false)
    }
  }

  const setActive = async (u: UserRow, active: boolean) => {
    setUserBusy(true)
    setUserError(undefined)
    try {
      const updated = await api.updateUser(u.id, { active })
      setUsers((prev) => prev.map((x) => (x.id === u.id ? updated : x)))
    } catch (err) {
      setUserError(err instanceof Error ? err.message : 'Could not change the status')
    } finally {
      setUserBusy(false)
    }
  }

  const resetPassword = async (u: UserRow) => {
    setUserBusy(true)
    setUserError(undefined)
    try {
      const { one_time_password } = await api.resetUserPassword(u.id)
      setResetOtps((prev) => ({ ...prev, [u.id]: one_time_password }))
      const updated = (await api.listUsers()).find((x) => x.id === u.id)
      if (updated) setUsers((prev) => prev.map((x) => (x.id === u.id ? updated : x)))
    } catch (err) {
      setUserError(err instanceof Error ? err.message : 'Could not reset the password')
    } finally {
      setUserBusy(false)
    }
  }

  const refreshKeys = async () => {
    setKeys(await api.listApiKeys())
  }

  const createKey = async (e: FormEvent) => {
    e.preventDefault()
    setKeysBusy(true)
    setKeysError(undefined)
    setCreatedKey(undefined)
    try {
      const made = await api.createApiKey({ name: newKeyName.trim(), mode: newKeyMode })
      setNewKeyName('')
      setCreatedKey(made)
      await refreshKeys()
    } catch (err) {
      setKeysError(err instanceof Error ? err.message : 'Could not create the key')
    } finally {
      setKeysBusy(false)
    }
  }

  const revokeKey = async (id: string) => {
    setKeysBusy(true)
    setKeysError(undefined)
    try {
      const updated = await api.revokeApiKey(id)
      setKeys((prev) => prev.map((k) => (k.id === id ? updated : k)))
      setRevokeFor(undefined)
    } catch (err) {
      setKeysError(err instanceof Error ? err.message : 'Could not revoke the key')
    } finally {
      setKeysBusy(false)
    }
  }

  const rotateKey = async (id: string) => {
    setKeysBusy(true)
    setKeysError(undefined)
    setCreatedKey(undefined)
    try {
      const made = await api.rotateApiKey(id, rotateChoice)
      setCreatedKey(made)
      setRotateFor(undefined)
      await refreshKeys()
    } catch (err) {
      setKeysError(err instanceof Error ? err.message : 'Could not rotate the key')
    } finally {
      setKeysBusy(false)
    }
  }

  const rotateSecret = async () => {
    setSecretBusy(true)
    setSecretError(undefined)
    setShownSecret(undefined)
    try {
      const { secret } = await api.rotateWebhookSecret()
      setShownSecret(secret)
      setSecretInfo(await api.getWebhookSecret())
    } catch (err) {
      setSecretError(err instanceof Error ? err.message : 'Could not rotate the secret')
    } finally {
      setSecretBusy(false)
    }
  }

  const sendTest = async (e: FormEvent) => {
    e.preventDefault()
    setTestBusy(true)
    setTestError(undefined)
    setTestResult(undefined)
    try {
      setTestResult(await api.sendTestWebhook(testUrl.trim()))
    } catch (err) {
      setTestError(err instanceof Error ? err.message : 'Could not send the test webhook')
    } finally {
      setTestBusy(false)
    }
  }

  const copy = async (key: string, text: string) => {
    try {
      await copyText(text)
      setCopied(key)
    } catch {
      setCopied(undefined)
    }
  }

  const otpRow = (key: string, otp: string) => (
    <p style={{ margin: 0, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <code className="ev-code">{otp}</code>
      <button type="button" className="ev-pill sm press" onClick={() => void copy(key, otp)}>
        {copied === key ? 'Copied' : 'Copy'}
      </button>
    </p>
  )

  return (
    <AdminLayout>
      <section style={{ display: 'grid', gap: 14 }}>
        <div className="ev-panel ev-pagehead">
          <div>
            <h1>Settings</h1>
            {updatedBy && (
              <p>
                Last changed by {updatedBy}
                {updatedAt ? `, ${formatTime(updatedAt)}` : ''}
              </p>
            )}
          </div>
        </div>
        <div className="ev-seg" role="group" aria-label="Settings sections">
          {SETTINGS_TABS.map((t) => (
            <button key={t.key} type="button" className="press" aria-pressed={tab === t.key} onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
        {tab === 'team' && (
          <p className="mute">People at your company who can open claims and decide them. Supervisors also manage settings.</p>
        )}
        {tab === 'claimant' && (
          <p className="mute">How the capture link looks to your customer.</p>
        )}
        {tab === 'reports' && (
          <p className="mute">When the report reaches your claims system, and how long photos and video are kept.</p>
        )}
        {tab === 'integrations' && (
          <p className="mute">
            For your IT team: keys and webhooks to connect your claims system. The{' '}
            <Link to="/developers">Developers</Link> page explains each step.
          </p>
        )}
        {tab === 'claimant' && (
        <section className="ev-card" aria-label="Brand">
          <h2>Brand</h2>
          {!loaded ? (
            <p className="mute">Loading…</p>
          ) : (
            <form
              style={{ display: 'grid', gap: 10 }}
              onSubmit={(e) => {
                e.preventDefault()
                void save()
              }}
            >
              <label className="ev-field" style={{ maxWidth: 420 }}>
                Brand color
                <span style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <input
                    type="color"
                    aria-label="Brand color"
                    value={brand}
                    disabled={!supervisor}
                    onChange={(e) => setBrand(e.target.value)}
                    style={{ width: 44, height: 36, padding: 2 }}
                  />
                  <span className="mute" style={{ fontWeight: 400 }}>
                    The claimant header and buttons use this color. (#RRGGBB)
                  </span>
                </span>
              </label>
              <div aria-hidden="true" style={{ border: '1px solid var(--line)', borderRadius: 12, maxWidth: 420 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '10px 12px' }}>
                  <span
                    style={{
                      display: 'grid',
                      placeItems: 'center',
                      width: 22,
                      height: 22,
                      borderRadius: 6,
                      fontSize: 12,
                      fontWeight: 800,
                      color: '#fff',
                      background: brand,
                    }}
                  >
                    D
                  </span>
                  <span style={{ fontSize: 14, fontWeight: 700 }}>Demo Mutual</span>
                  <span className="mute" style={{ marginLeft: 'auto', fontSize: 12.5 }}>
                    DM-2026-0415
                  </span>
                </div>
                <div style={{ margin: '0 12px 4px', height: 4, borderRadius: 3, overflow: 'hidden', background: '#e3e9f1' }}>
                  <div style={{ height: '100%', width: '33%', borderRadius: 3, background: brand }} />
                </div>
                <div className="mute" style={{ padding: '0 12px 10px', fontSize: 12 }}>
                  Step 1 of 5: What happened
                </div>
              </div>
              {!supervisor && <p className="mute">Only a supervisor can change these.</p>}
              {error && (
                <p role="alert" className="ev-err" style={{ margin: 0 }}>
                  {error}
                </p>
              )}
              {saved && (
                <p className="ev-ok" style={{ margin: 0 }}>
                  Saved.
                </p>
              )}
              {supervisor && (
                <div>
                  <button type="submit" disabled={saving} className="ev-pill primary press">
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}
            </form>
          )}
        </section>
        )}
        {tab === 'reports' && (
        <>
        <section className="ev-card" aria-label="Report delivery">
          <h2>Report delivery</h2>
          {!loaded ? (
            <p className="mute">Loading…</p>
          ) : (
            <form
              style={{ display: 'grid', gap: 10 }}
              onSubmit={(e) => {
                e.preventDefault()
                void save()
              }}
            >
              <label className="ev-radio">
                <input
                  type="radio"
                  name="delivery"
                  value="on_decision"
                  checked={mode === 'on_decision'}
                  disabled={!supervisor}
                  onChange={() => setMode('on_decision')}
                />
                <span>
                  <strong>After my decision (reviewed report only)</strong>
                  <br />
                  <span className="mute">Nothing is sent until you decide. Your decision sends the reviewed report.</span>
                </span>
              </label>
              <label className="ev-radio">
                <input
                  type="radio"
                  name="delivery"
                  value="on_ready"
                  checked={mode === 'on_ready'}
                  disabled={!supervisor}
                  onChange={() => setMode('on_ready')}
                />
                <span>
                  <strong>As soon as the analysis finishes, then my decision as an update</strong>
                  <br />
                  <span className="mute">The draft report is sent at once; your decision follows as an update.</span>
                </span>
              </label>
              {!supervisor && loaded && <p className="mute">Only a supervisor can change these.</p>}
              {error && (
                <p role="alert" className="ev-err" style={{ margin: 0 }}>
                  {error}
                </p>
              )}
              {saved && (
                <p className="ev-ok" style={{ margin: 0 }}>
                  Saved.
                </p>
              )}
              {supervisor && (
                <div>
                  <button type="submit" disabled={saving} className="ev-pill primary press">
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}
            </form>
          )}
        </section>
        <section className="ev-card" aria-label="Data retention">
          <h2>Data retention</h2>
          {!loaded ? (
            <p className="mute">Loading…</p>
          ) : (
            <form
              style={{ display: 'grid', gap: 10 }}
              onSubmit={(e) => {
                e.preventDefault()
                void save()
              }}
            >
              <label className="ev-field" style={{ maxWidth: 420 }}>
                Delete photos and video
                <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 400 }}>
                  <input
                    type="number"
                    aria-label="Retention days"
                    value={retention}
                    min={30}
                    max={3650}
                    disabled={!supervisor}
                    onChange={(e) => setRetention(e.target.value)}
                    className="ev-input"
                    style={{ width: 96 }}
                  />
                  days after the decision
                </span>
              </label>
              {!supervisor && <p className="mute">Only a supervisor can change these.</p>}
              {error && (
                <p role="alert" className="ev-err" style={{ margin: 0 }}>
                  {error}
                </p>
              )}
              {saved && (
                <p className="ev-ok" style={{ margin: 0 }}>
                  Saved.
                </p>
              )}
              {supervisor && (
                <div>
                  <button type="submit" disabled={saving} className="ev-pill primary press">
                    {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}
            </form>
          )}
        </section>
        </>
        )}
        {tab === 'team' && supervisor && (
          <section className="ev-card" aria-label="Users">
            <h2>Users</h2>
            <div className="ev-tablewrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.id}>
                      <td>{u.name}</td>
                      <td>{u.email}</td>
                      <td>
                        <select
                          aria-label={`Role for ${u.name}`}
                          value={u.role}
                          disabled={userBusy || (me != null && u.id === me.id)}
                          onChange={(e) => void changeRole(u, e.target.value as 'adjuster' | 'supervisor')}
                          className="ev-input"
                        >
                          <option value="adjuster">adjuster</option>
                          <option value="supervisor">supervisor</option>
                        </select>
                      </td>
                      <td>
                        {u.active ? 'Active' : 'Deactivated'}
                        {u.must_change_password && <span className="mute"> · password reset pending</span>}
                      </td>
                      <td>
                        <span style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                          <button
                            type="button"
                            className="ev-pill sm press"
                            disabled={userBusy || (me != null && u.id === me.id)}
                            onClick={() => void setActive(u, !u.active)}
                          >
                            {u.active ? 'Deactivate' : 'Reactivate'}
                          </button>
                          <button
                            type="button"
                            className="ev-pill sm press"
                            disabled={userBusy}
                            onClick={() => void resetPassword(u)}
                          >
                            Reset password
                          </button>
                        </span>
                        {resetOtps[u.id] && (
                          <span style={{ display: 'block', marginTop: 6 }}>{otpRow(`reset-${u.id}`, resetOtps[u.id])}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {userError && (
              <p role="alert" className="ev-err" style={{ margin: 0 }}>
                {userError}
              </p>
            )}
            <form
              style={{ display: 'grid', gap: 10, borderTop: '1px solid var(--line)', paddingTop: 12 }}
              onSubmit={addUser}
            >
              <h3 style={{ fontSize: 13, fontWeight: 700 }}>Add user</h3>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <label className="ev-field" style={{ flex: '1 1 160px' }}>
                  Name
                  <input value={newName} onChange={(e) => setNewName(e.target.value)} />
                </label>
                <label className="ev-field" style={{ flex: '1 1 200px' }}>
                  Email
                  <input value={newEmail} onChange={(e) => setNewEmail(e.target.value)} autoComplete="off" />
                </label>
                <label className="ev-field" style={{ flex: '0 1 140px' }}>
                  Role
                  <select value={newRole} onChange={(e) => setNewRole(e.target.value as 'adjuster' | 'supervisor')}>
                    <option value="adjuster">adjuster</option>
                    <option value="supervisor">supervisor</option>
                  </select>
                </label>
              </div>
              <div>
                <button
                  type="submit"
                  disabled={userBusy || !newName || !newEmail}
                  className="ev-pill primary press"
                >
                  {userBusy ? 'Adding…' : 'Add user'}
                </button>
              </div>
              {newUserOtp && (
                <div style={{ fontSize: 12 }}>
                  <p style={{ margin: 0 }}>One-time password (shown once):</p>
                  <span style={{ display: 'block', marginTop: 6 }}>{otpRow('new-user', newUserOtp)}</span>
                  <p className="mute" style={{ margin: '6px 0 0' }}>
                    Give it to them in person or by phone; they choose their own at first sign-in.
                  </p>
                </div>
              )}
            </form>
          </section>
        )}
        {tab === 'integrations' && supervisor && (
          <section className="ev-card" aria-label="Integrations">
            <h2>Integrations</h2>
            <p className="mute" style={{ fontSize: 12 }}>
              API keys for your insurer&apos;s IT, the webhook secret, and a test sender.{' '}
              <Link to="/developers">Developers</Link> documents it all for them.
            </p>
            <h3 style={{ fontSize: 13, fontWeight: 700 }}>API keys</h3>
            {keys.length === 0 ? (
              <p className="mute">No API keys yet.</p>
            ) : (
              <div className="ev-tablewrap">
                <table>
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Mode</th>
                      <th>Prefix</th>
                      <th>Status</th>
                      <th>Last used</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keys.map((k) => (
                      <tr key={k.id}>
                        <td>{k.name}</td>
                        <td>{k.mode}</td>
                        <td>
                          <code className="ev-code">{k.prefix}</code>
                        </td>
                        <td>{k.status}</td>
                        <td>{formatTime(k.last_used_at)}</td>
                        <td>
                          <span style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                            <button
                              type="button"
                              className="ev-pill sm press"
                              disabled={keysBusy || k.status === 'revoked' || k.status === 'expired'}
                              onClick={() => {
                                setRotateFor(k.id)
                                setRevokeFor(undefined)
                              }}
                            >
                              Rotate
                            </button>
                            <button
                              type="button"
                              className="ev-pill sm press"
                              disabled={keysBusy || k.status === 'revoked'}
                              onClick={() => {
                                setRevokeFor(k.id)
                                setRotateFor(undefined)
                              }}
                            >
                              Revoke
                            </button>
                          </span>
                          {rotateFor === k.id && (
                            <span style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, marginTop: 6, fontSize: 12 }}>
                              <label>
                                Old key stops{' '}
                                <select
                                  value={rotateChoice}
                                  onChange={(e) => setRotateChoice(e.target.value as 'now' | '1h' | '24h')}
                                  className="ev-input"
                                >
                                  <option value="now">now</option>
                                  <option value="1h">in 1 hour</option>
                                  <option value="24h">in 24 hours</option>
                                </select>
                              </label>
                              <button
                                type="button"
                                className="ev-pill sm primary press"
                                disabled={keysBusy}
                                onClick={() => void rotateKey(k.id)}
                              >
                                Confirm
                              </button>
                              <button type="button" className="ev-pill sm press" onClick={() => setRotateFor(undefined)}>
                                Cancel
                              </button>
                            </span>
                          )}
                          {revokeFor === k.id && (
                            <span style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 6, marginTop: 6, fontSize: 12 }}>
                              Revoke this key?
                              <button
                                type="button"
                                className="ev-pill sm primary press"
                                disabled={keysBusy}
                                onClick={() => void revokeKey(k.id)}
                              >
                                Confirm
                              </button>
                              <button type="button" className="ev-pill sm press" onClick={() => setRevokeFor(undefined)}>
                                Cancel
                              </button>
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {keysError && (
              <p role="alert" className="ev-err" style={{ margin: 0 }}>
                {keysError}
              </p>
            )}
            <form style={{ display: 'grid', gap: 10, borderTop: '1px solid var(--line)', paddingTop: 12 }} onSubmit={createKey}>
              <h3 style={{ fontSize: 13, fontWeight: 700, margin: 0 }}>New key</h3>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
                <label className="ev-field" style={{ flex: '1 1 200px' }}>
                  Name
                  <input
                    value={newKeyName}
                    onChange={(e) => setNewKeyName(e.target.value)}
                    maxLength={60}
                    placeholder="Claims system"
                  />
                </label>
                <label className="ev-field" style={{ flex: '0 1 140px' }}>
                  Mode
                  <select value={newKeyMode} onChange={(e) => setNewKeyMode(e.target.value as 'live' | 'test')}>
                    <option value="live">live</option>
                    <option value="test">test</option>
                  </select>
                </label>
              </div>
              <div>
                <button type="submit" disabled={keysBusy || !newKeyName.trim()} className="ev-pill primary press">
                  {keysBusy ? 'Creating…' : 'Create key'}
                </button>
              </div>
              {createdKey && (
                <div style={{ fontSize: 12 }}>
                  <p style={{ margin: 0 }}>New key (shown once):</p>
                  <span style={{ display: 'block', marginTop: 6 }}>{otpRow(`key-${createdKey.id}`, createdKey.key)}</span>
                  <p className="mute" style={{ margin: '6px 0 0' }}>
                    Store it now: it is not shown again.
                  </p>
                </div>
              )}
            </form>
            <h3 style={{ fontSize: 13, fontWeight: 700 }}>Webhook secret</h3>
            {secretInfo ? (
              <p style={{ margin: 0, fontSize: 13 }}>
                Ends in <code className="ev-code">{secretInfo.last4}</code>
                {secretInfo.rotated_at ? ` · rotated ${formatTime(secretInfo.rotated_at)}` : ' · never rotated'}
              </p>
            ) : (
              <p className="mute">Loading…</p>
            )}
            <div>
              <button type="button" disabled={secretBusy} className="ev-pill sm press" onClick={() => void rotateSecret()}>
                {secretBusy ? 'Rotating…' : 'Rotate secret'}
              </button>
            </div>
            {shownSecret && (
              <div style={{ fontSize: 12 }}>
                <p style={{ margin: 0 }}>New secret (shown once):</p>
                <span style={{ display: 'block', marginTop: 6 }}>{otpRow('webhook-secret', shownSecret)}</span>
                <p className="mute" style={{ margin: '6px 0 0' }}>
                  Store it now: it is not shown again. Deliveries from now on are signed with it.
                </p>
              </div>
            )}
            {secretError && (
              <p role="alert" className="ev-err" style={{ margin: 0 }}>
                {secretError}
              </p>
            )}
            <h3 style={{ fontSize: 13, fontWeight: 700 }}>Send a test webhook</h3>
            <form style={{ display: 'grid', gap: 10 }} onSubmit={sendTest}>
              <label className="ev-field" style={{ maxWidth: 420 }}>
                Callback URL
                <input
                  value={testUrl}
                  onChange={(e) => setTestUrl(e.target.value)}
                  placeholder="https://insurer.example/hook"
                />
              </label>
              <div>
                <button type="submit" disabled={testBusy || !testUrl.trim()} className="ev-pill primary press">
                  {testBusy ? 'Sending…' : 'Send ping'}
                </button>
              </div>
              {testResult && (
                <p style={{ margin: 0, fontSize: 12 }} role="status">
                  {testResult.ok
                    ? `Delivered · HTTP ${testResult.status_code} · ${testResult.ms} ms`
                    : testResult.error
                      ? `Failed · ${testResult.error}`
                      : `Failed · HTTP ${testResult.status_code} · ${testResult.ms} ms`}
                </p>
              )}
              {testError && (
                <p role="alert" className="ev-err" style={{ margin: 0 }}>
                  {testError}
                </p>
              )}
            </form>
          </section>
        )}
      </section>
    </AdminLayout>
  )
}

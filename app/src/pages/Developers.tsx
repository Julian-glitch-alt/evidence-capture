import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type DevelopersInfo } from '../api'

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? ''
}

export default function Developers() {
  const [info, setInfo] = useState<DevelopersInfo>()
  const [error, setError] = useState<string>()
  const [copied, setCopied] = useState<string>()

  useEffect(() => {
    api
      .developers()
      .then(setInfo)
      .catch((e) => setError(e instanceof Error ? e.message : 'Could not load the developer docs'))
  }, [])

  const copy = async (key: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
    } catch {
      setCopied(undefined)
    }
  }

  const copyButton = (key: string, text: string) => (
    <button type="button" className="ev-pill sm press" onClick={() => void copy(key, text)}>
      {copied === key ? 'Copied' : 'Copy'}
    </button>
  )

  const base = window.location.origin
  const limits = info?.limits
  const events = info?.events ?? {}
  const order = ['evidence.ready', 'decision.recorded', 'ping']

  return (
    <div className="ev" style={{ minHeight: '100%', paddingBlock: 16, paddingInline: 16 }}>
      <div className="ev-public">
        <header className="ev-topbar">
          <Link to="/" className="ev-brandline" aria-label="Evidence Desk home">
            <i aria-hidden="true">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6l7-3z" />
                <path d="M8.6 12.2l2.3 2.3 4.6-4.7" />
              </svg>
            </i>
            <span>
              <b>Evidence Desk</b>
              <small>Demo Mutual Insurance</small>
            </span>
          </Link>
          <span className="ev-spacer" />
          <nav className="ev-toplinks" aria-label="Public">
            <Link to="/verify">Verify a sealed package</Link>
            <Link to="/developers" className="on" aria-current="page">
              Developers
            </Link>
            <a href="/api/docs">API reference</a>
          </nav>
        </header>

        <div className="ev-panel ev-pagehead">
          <div>
            <h1>Developers</h1>
            <p>
              Connect your claims system in an afternoon: your own keys, a test mode that behaves
              like the real thing, and signed webhooks. No sales call needed.
            </p>
          </div>
        </div>

        {error && (
          <p role="alert" className="ev-err" style={{ padding: '0 6px' }}>
            {error}
          </p>
        )}
        {!info && !error && (
          <p className="mute" style={{ padding: '0 6px' }}>
            Loading…
          </p>
        )}
        {info && (
          <article className="ev-docs" aria-label="Developer documentation">
            <section aria-label="Five-minute quickstart">
              <h2>Five-minute quickstart</h2>
              <ol>
                <li>
                  Ask your supervisor for a test key. It starts with <code>ek_test_</code>.
                </li>
                <li>
                  Create a capture request:
                  <pre>
{`curl -X POST ${base}/api/v1/capture-requests \\
  -H "Authorization: Bearer [REDACTED]" \\
  -H "Content-Type: application/json" \\
  -d '{"insurer_claim_number": "DM-2026-0001", "policy_number": "DEMO-AUTO-1001", "callback_url": "https://insurer.example/hook", "claimant": {"name": "Jordan Avery"}, "loss": {"date": "2026-10-27", "description": "Backed into a bollard."}}'`}
                  </pre>
                </li>
                <li>
                  Open the <code>capture_url</code> from the reply and complete the capture.
                </li>
                <li>
                  Your <code>callback_url</code> receives an <code>evidence.ready</code> POST, signed
                  with your webhook secret.
                </li>
                <li>Verify the signature with a sample below.</li>
              </ol>
            </section>

            <section aria-label="Events">
              <h2>Events</h2>
              <p>
                The exact shapes, with sample values. Your bodies carry your claim&apos;s ids
                and your <code>test_mode</code>.
              </p>
              {order
                .filter((name) => name in events)
                .map((name) => (
                  <div key={name}>
                    <div className="row">
                      <h3>
                        <code>{name}</code>
                      </h3>
                      {copyButton(`event-${name}`, pretty(events[name]))}
                    </div>
                    <pre>{pretty(events[name])}</pre>
                  </div>
                ))}
            </section>

            <section aria-label="Signature verification">
              <h2>Signature verification</h2>
              <p>
                Every webhook carries an <code>X-Evidence-Signature</code> header of the
                form <code>t=…,v1=…</code>: the send time and the HMAC-SHA256 of{' '}
                <code>{'"<time>." + body'}</code> under your webhook secret. Check the shape, the signature with a
                constant-time compare, and refuse anything more than 5 minutes old. The samples below run
                as printed.
              </p>
              <div>
                <div className="row">
                  <h3>Python (standard library only)</h3>
                  {copyButton('verify-python', info.verify.python)}
                </div>
                <pre>{info.verify.python}</pre>
              </div>
              <div>
                <div className="row">
                  <h3>Node (crypto only)</h3>
                  {copyButton('verify-node', info.verify.node)}
                </div>
                <pre>{info.verify.node}</pre>
              </div>
            </section>

            <section aria-label="Retries and ordering">
              <h2>Retries and ordering</h2>
              <p>
                A delivery that fails is retried up to {limits?.retries} times: the first retry
                after {limits?.first_retry_seconds} seconds, the wait between tries never more
                than {limits?.max_retry_wait_seconds} seconds. Messages for one claim go in
                order: a message still waiting holds back the later ones.
              </p>
            </section>

            <section aria-label="Idempotency">
              <h2>Idempotency</h2>
              <p>
                Sending the same <code>insurer_claim_number</code> twice
                returns the existing case: the second answer is the same{' '}
                <code>case_id</code> with a 200 status, and
                the first <code>callback_url</code> stays.
                Test and live are separate: the same number under a test key and a live key
                makes two cases.
              </p>
            </section>

            <section aria-label="Statuses">
              <h2>Statuses</h2>
              <p>
                The capture-request reply carries <code>case_id</code>,{' '}
                <code>insurer_claim_number</code>, <code>status</code>,{' '}
                <code>test_mode</code>, <code>capture_url</code>,{' '}
                <code>expires_at</code> and <code>report_url</code>. The status follows
                the claim&apos;s progress; <code>report_url</code>{' '}
                is null until the report exists.
              </p>
            </section>

            <section aria-label="Sending the policy with the request">
              <h2>Sending the policy with the request</h2>
              <p>
                A capture request can carry the <code>policy</code> the photos are checked against: state, dates,
                holder, drivers, vehicles and coverages, shaped like the policy in our seed file. We freeze it with
                the claim as evidence; we never keep it as a policy record.
              </p>
              <pre>{`{
  "insurer_claim_number": "DM-2026-0001",
  "policy_number": "DEMO-AUTO-1001",
  "policy": {
    "state": "CA",
    "effective": "2026-10-01",
    "expires": "2027-10-01",
    "holder": { "name": "Jordan Avery" },
    "drivers": [{ "name": "Jordan Avery" }],
    "vehicles": [
      { "year": 2021, "make": "Honda", "model": "Civic", "plate": "7ABC123", "vin": "1HGCV1F30MA123456" }
    ],
    "coverages": {
      "collision": { "deductible": 500 },
      "liability": { "limits": "100/300/100" }
    }
  }
}`}</pre>
            </section>

            <section aria-label="Rate limits">
              <h2>Rate limits</h2>
              <p>
                The integration API takes {limits?.api_per_minute} requests a minute per address.
                Past that it answers 429 with a <code>Retry-After</code> wait.
              </p>
            </section>

            <section aria-label="Test mode">
              <h2>Test mode</h2>
              <p>
                Keys starting with <code>ek_test_</code> make test claims: capture, seal, analysis and webhooks run for
                real, and replies and webhook bodies carry <code>&quot;test_mode&quot;: true</code>. Test
                claims sit in their own lane and never count. The same number under a test key
                and a live key makes two cases, and a key of one mode asking for the other
                mode&apos;s case gets a 404.
              </p>
            </section>

            <p>
              <a href="/api/docs">Full API reference</a>
            </p>
          </article>
        )}
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type ContactModel } from '../api'

/** Public page: what the product collects and why, in plain English. */
export default function Privacy() {
  const [email, setEmail] = useState<string | null>(null)
  const [model, setModel] = useState<ContactModel | null>(null)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    api
      .contact()
      .then((c) => {
        setEmail(c.email)
        setModel(c.model)
      })
      .catch(() => {
        setEmail(null)
        setModel(null)
      })
      .finally(() => setLoaded(true))
  }, [])

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
            <Link to="/developers">Developers</Link>
            <a href="/api/docs">API reference</a>
          </nav>
        </header>

        <div className="ev-panel ev-pagehead">
          <div>
            <h1>Privacy notice</h1>
            <p>Prototype · last updated 3 November 2026</p>
          </div>
        </div>

        <article className="ev-docs" aria-label="Privacy notice">
          <section aria-label="What we collect">
            <h2>What we collect</h2>
            <p>
              Your answers in the loss notice: your name, your contact details, and what happened. The photos and
              video you take in the capture link, and your finger signature. The VIN and plate read from your
              photos, the upload times, and basic information your phone reports: the browser and the camera.
            </p>
            <p>Your IP address is kept for a short time to stop abuse.</p>
            <p>
              We do not collect your location. This page cannot use GPS. You cannot upload photos from your
              gallery: photos are taken live in this page.
            </p>
          </section>

          <section aria-label="Why we collect it">
            <h2>Why we collect it</h2>
            <p>
              To check the evidence for the claim the insurer opened, and to give the insurer&apos;s adjuster a
              sealed report. A person at the insurer makes every decision. The AI never approves or denies a
              claim.
            </p>
          </section>

          <section aria-label="How the AI reads it">
            <h2>How the AI reads it</h2>
            {model?.provider === 'meta' ? (
              <p>
                Your photos and your written statement are read by Muse Spark 1.3, an AI model from Meta,
                through the Meta Model API. On its free credits Meta may use what it receives, so this
                trial uses no customer data. Driver&apos;s license photos are never read by the AI.
              </p>
            ) : model?.provider === 'fireworks' ? (
              <p>
                Your photos and your written statement are read by Muse Spark Open, an AI model from Meta. It is
                run by Fireworks AI with zero data retention: Fireworks does not store them or train on them.
                Driver&apos;s license photos are never read by the AI.
              </p>
            ) : (
              <p>
                Your photos and your written statement are read by an AI model from Meta.
                Driver&apos;s license photos are never read by the AI.
              </p>
            )}
          </section>

          <section aria-label="Where it is kept">
            <h2>Where it is kept</h2>
            <p>
              The claim record is kept in a Postgres database in the US (Neon, us-east-1). Files are kept in
              Cloudflare R2. The service runs on Modal in the US. Every file is sealed with a digital
              signature when it arrives.
            </p>
          </section>

          <section aria-label="Who can see it">
            <h2>Who can see it</h2>
            <p>
              The insurer&apos;s claims staff who use this console, and the insurer&apos;s own systems that
              receive the report. During this trial, testers from different companies share one workspace and
              can see each other&apos;s trial claims: do not use customer data.
            </p>
          </section>

          <section aria-label="How long it is kept">
            <h2>How long it is kept</h2>
            <p>
              Photos, video and report files are deleted 90 days after the insurer&apos;s decision. The insurer
              can set 30 to 3650 days instead. The claim record, with your answers and the check results,
              stays with the insurer&apos;s claim.
            </p>
          </section>

          <section aria-label="Contact">
            <h2>Contact</h2>
            {loaded && email && (
              <p>
                Questions or deletion requests: <a href={`mailto:${email}`}>{email}</a>
              </p>
            )}
            {loaded && !email && <p>Ask the insurer that sent you the link.</p>}
            {!loaded && <p className="mute">Loading…</p>}
          </section>
        </article>
      </div>
    </div>
  )
}

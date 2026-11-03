import { useState } from 'react'
import { Link } from 'react-router-dom'
import { formatTime, verifyPackage, type VerifyResult } from '../api'

interface ManifestEntry {
  file: string
  sha256: string
}

interface ManifestInfo {
  keyId: string | null
  files: ManifestEntry[]
  answersSha256: string | null
  policySha256: string | null
  removed: string[]
}

/** Read manifest.json (and removal.json) straight from the uploaded zip, so the
 *  confirmation document can list every sealed file with its fingerprint. The
 *  server writes stored (uncompressed) entries; anything else is left to the
 *  server verdict and the table is omitted. */
async function readSealedManifest(zip: File): Promise<ManifestInfo | null> {
  try {
    const data = new Uint8Array(await zip.arrayBuffer())
    const view = new DataView(data.buffer, data.byteOffset, data.byteLength)
    const text = new TextDecoder()
    const bytesOf = (name: string): Uint8Array | null => {
      let pos = 0
      while (pos + 30 <= data.length) {
        const sig = data.indexOf(0x50, pos)
        if (sig < 0 || sig + 30 > data.length) return null
        pos = sig
        if (view.getUint32(pos, true) !== 0x04034b50) {
          pos += 1
          continue
        }
        const method = view.getUint16(pos + 8, true)
        const size = view.getUint32(pos + 18, true)
        const nameLen = view.getUint16(pos + 26, true)
        const extraLen = view.getUint16(pos + 28, true)
        const nameStart = pos + 30
        const dataStart = nameStart + nameLen + extraLen
        if (nameStart > data.length || dataStart > data.length || dataStart + size > data.length) return null
        if (text.decode(data.subarray(nameStart, nameStart + nameLen)) === name) {
          if (method !== 0) return null
          return data.slice(dataStart, dataStart + size)
        }
        pos = dataStart + size
      }
      return null
    }
    const raw = bytesOf('manifest.json')
    if (!raw) return null
    const parsed: unknown = JSON.parse(text.decode(raw))
    if (typeof parsed !== 'object' || parsed === null) return null
    const manifest = parsed as Record<string, unknown>
    const files: ManifestEntry[] = []
    if (Array.isArray(manifest.items)) {
      for (const item of manifest.items) {
        if (typeof item === 'object' && item !== null) {
          const rec = item as Record<string, unknown>
          if (typeof rec.file === 'string' && typeof rec.sha256 === 'string') files.push({ file: rec.file, sha256: rec.sha256 })
        }
      }
    }
    const strOrNull = (v: unknown) => (typeof v === 'string' ? v : null)
    let removed: string[] = []
    const removalRaw = bytesOf('removal.json')
    if (removalRaw) {
      try {
        const receipt: unknown = JSON.parse(text.decode(removalRaw))
        if (typeof receipt === 'object' && receipt !== null) {
          const list = (receipt as Record<string, unknown>).removed
          if (Array.isArray(list)) {
            for (const e of list) {
              if (typeof e === 'object' && e !== null) {
                const f = (e as Record<string, unknown>).file
                if (typeof f === 'string') removed.push(f)
              }
            }
          }
        }
      } catch {
        removed = []
      }
    }
    return {
      keyId: strOrNull(manifest.key_id),
      files,
      answersSha256: strOrNull(manifest.answers_sha256),
      policySha256: strOrNull(manifest.policy_snapshot_sha256),
      removed,
    }
  } catch {
    return null
  }
}

/** Public page: anyone holding a sealed claim package can check it was not changed. */
export default function Verify() {
  const [result, setResult] = useState<VerifyResult>()
  const [manifest, setManifest] = useState<ManifestInfo | null>(null)
  const [fileName, setFileName] = useState<string>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const [over, setOver] = useState(false)

  const check = async (file: File | undefined) => {
    if (!file) return
    setBusy(true)
    setError(undefined)
    setResult(undefined)
    setManifest(null)
    setFileName(file.name)
    try {
      setResult(await verifyPackage(file))
      setManifest(await readSealedManifest(file))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not check this file')
    } finally {
      setBusy(false)
    }
  }

  const removedCount = result?.removed ?? 0
  const title = !result
    ? ''
    : !result.valid
      ? 'This package does not match its seal'
      : removedCount > 0
        ? result.removed_reason === 'retention'
          ? 'Files removed by retention'
          : 'Files removed at a deletion request'
        : 'Package verified'
  const removedSet = new Set(manifest?.removed ?? [])
  const showRemovedMarks = !!result?.valid && removedCount > 0
  const fileRows: { file: string; sha256: string }[] = manifest
    ? [
        ...manifest.files,
        ...(manifest.answersSha256 ? [{ file: 'answers.json', sha256: manifest.answersSha256 }] : []),
        ...(manifest.policySha256 ? [{ file: 'policy_snapshot.json', sha256: manifest.policySha256 }] : []),
      ]
    : []
  const flagged = (file: string) => (result?.problems ?? []).some((p) => p.includes(file))
  const sections = { count: 0 } // numbers the document's sections in render order
  const nextSection = () => ++sections.count

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
            <Link to="/verify" className="on" aria-current="page">
              Verify a sealed package
            </Link>
            <Link to="/developers">Developers</Link>
            <a href="/api/docs">API reference</a>
          </nav>
        </header>

        <div className="ev-panel ev-pagehead">
          <div>
            <h1>Verify a sealed claim</h1>
            <p>
              Drop the claim package (the .zip an adjuster downloads). The check runs on our server against our public
              sealing key and tells you whether any file, answer or record changed after the claim was sent.
            </p>
          </div>
        </div>

        <label
          onDragOver={(e) => {
            e.preventDefault()
            setOver(true)
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setOver(false)
            void check(e.dataTransfer.files[0])
          }}
          className={`ev-drop press${over ? ' over' : ''}`}
        >
          <input
            id="package"
            type="file"
            accept=".zip,application/zip"
            className="sr-only"
            onChange={(e) => void check(e.target.files?.[0])}
          />
          <b>{busy ? 'Checking…' : 'Drop the .zip here, or choose a file'}</b>
          {fileName && <span>{fileName}</span>}
        </label>

        {error && (
          <p role="alert" className="ev-err">
            {error}
          </p>
        )}

        {result && (
          <div className="ev-desk">
            <article className="ev-page" data-testid="verify-result" aria-label="Package check result">
              <div className="ev-runhead">
                <span>Evidence Desk · Verify</span>
                <span>{fileName ?? 'Sealed package'}</span>
              </div>
              <h2 className="ev-wtitle">{title}</h2>
              {result.valid ? (
                <p className="ev-wsub">
                  Unchanged since it was sealed. Claim {result.claim_id}, sealed {formatTime(result.sealed_at)}. All{' '}
                  {result.items} files, the answers and the policy snapshot match the seal.
                </p>
              ) : (
                <p className="ev-wsub">
                  This package does not match its seal.{result.claim_id ? ` Claim ${result.claim_id}.` : ''} What changed
                  is listed below.
                </p>
              )}
              {removedCount > 0 && (
                <p className="ev-wsub">
                  {result.removed} files were removed on {formatTime(result.removed_at ?? null)} (
                  {result.removed_reason === 'retention' ? 'by the retention policy' : 'at a deletion request'}); their
                  fingerprints are still sealed.
                </p>
              )}
              {!result.valid && (
                <section className="ev-wsec">
                  <h3>
                    {nextSection()}.&ensp;What changed
                  </h3>
                  <ul>
                    {result.problems.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                </section>
              )}
              {fileRows.length > 0 && (
                <section className="ev-wsec">
                  <h3>
                    {nextSection()}.&ensp;Files and their sealed fingerprints
                  </h3>
                  <table className="ev-wtable">
                    <tbody>
                      {fileRows.map((row) => (
                        <tr key={row.file}>
                          <th scope="row">{row.file}</th>
                          <td className="hash">
                            {row.sha256}
                            {flagged(row.file) && (
                              <>
                                <br />
                                <span>Does not match the seal.</span>
                              </>
                            )}
                            {showRemovedMarks && removedSet.has(row.file) && !flagged(row.file) && (
                              <>
                                <br />
                                <span>Removed; the fingerprint is still sealed.</span>
                              </>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="mute" style={{ fontSize: 12.5, marginTop: 8 }}>
                    Fingerprints (SHA-256) as sealed. A changed or added file breaks the seal; a removed file stays
                    listed here because its fingerprint is part of the seal.
                  </p>
                </section>
              )}
              <section className="ev-wsec">
                <h3>
                  {nextSection()}.&ensp;What was checked
                </h3>
                <p className="ev-wdecl">
                  When a claim is sent, our server signs a list of every file it received, with each file&apos;s
                  fingerprint (SHA-256), the answers, the policy as it stood, and the claim&apos;s history. Any later
                  change to any of them, by anyone including us, breaks the seal.
                </p>
                <p className="ev-wdecl" style={{ marginTop: 8 }}>
                  It does not prove that a photo shows what it claims to show. It proves these are the exact files the
                  customer&apos;s phone sent through the live capture page.
                </p>
              </section>
              <section className="ev-wsec">
                <h3>
                  {nextSection()}.&ensp;Seal
                </h3>
                <div className="ev-wsign">
                  <div>
                    <div className="sigline">
                      Sealed {formatTime(result.sealed_at)}
                      <br />
                      Seal key {manifest?.keyId ?? 'unknown'} · Ed25519
                    </div>
                  </div>
                  <div className="ev-stamp" aria-label="Sealed">
                    <span>
                      <b>SEALED</b>
                      {(result.sealed_at ? formatTime(result.sealed_at) : '').split(',')[0]}
                      <br />
                      Ed25519
                    </span>
                  </div>
                </div>
              </section>
              <div className="ev-runfoot">
                <span>Evidence Desk · public package check</span>
                <span>
                  Seal key <span className="mono">{manifest?.keyId ?? '—'}</span>
                </span>
              </div>
            </article>
          </div>
        )}
      </div>
    </div>
  )
}

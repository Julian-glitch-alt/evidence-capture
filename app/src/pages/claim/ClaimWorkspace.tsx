import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, type ClaimDetail } from '../../api'
import PhotoViewer, { type ViewerPhoto } from '../../components/PhotoViewer'
import LossNotice from '../../components/LossNotice'
import ReviewMemo from '../../components/ReviewMemo'
import DecisionCard, { MoreActionsCard, NotesCard, ReportFilesCard } from '../../components/DecisionCard'
import { holderOf, isNeedsLane, orderedFindings, photoLabel, vehicleOf } from './shared'

function sortNeeds(ids: { id: string; overdue?: boolean; review_due?: string | null; created_at: string }[]) {
  return [...ids].sort((a, b) => {
    const ao = a.overdue ? 0 : 1
    const bo = b.overdue ? 0 : 1
    if (ao !== bo) return ao - bo
    const ad = a.review_due ?? ''
    const bd = b.review_due ?? ''
    if (ad !== bd) {
      if (!ad) return 1
      if (!bd) return -1
      return ad < bd ? -1 : 1
    }
    return (b.created_at ?? '') < (a.created_at ?? '') ? -1 : 1
  })
}

export default function ClaimWorkspace({
  claim,
  onUpdate,
}: {
  claim: ClaimDetail
  onUpdate: (c: ClaimDetail) => void
}) {
  const navigate = useNavigate()
  const [viewerId, setViewerId] = useState<string | null>(null)
  const [selectedArea, setSelectedArea] = useState<string | null>(null)
  const [next, setNext] = useState<{ id: string; index: number; total: number } | null>(null)

  const findings = useMemo(() => orderedFindings(claim), [claim])
  const holder = holderOf(claim)
  const vehicle = vehicleOf(claim)

  const photos: ViewerPhoto[] = useMemo(
    () =>
      claim.items
        .filter((i) => !i.superseded && i.kind !== 'video')
        .map((item) => ({ item, label: photoLabel(claim, item) })),
    [claim],
  )
  const damagesByItem = useMemo(() => {
    const out: Record<string, ReturnType<typeof damagesForItem>> = {}
    for (const item of claim.items) out[item.id] = damagesForItem(claim, item.id)
    return out
  }, [claim])

  useEffect(() => {
    api
      .queue()
      .then((queue) => {
        const needs = sortNeeds(
          queue.groups
            .flatMap((g) => g.claims)
            .filter((c) => isNeedsLane(c.lane)),
        )
        const idx = needs.findIndex((c) => c.id === claim.id)
        if (idx >= 0 && idx < needs.length - 1) {
          setNext({ id: needs[idx + 1].id, index: idx + 2, total: needs.length })
        } else {
          setNext(null)
        }
      })
      .catch(() => undefined)
  }, [claim.id])

  return (
    <section style={{ display: 'grid', gap: 14 }}>
      <div className="ev-crumbs">
        <button type="button" className="press" onClick={() => navigate('/')}>
          Today
        </button>
        <span>/</span>
        <span className="ev-cid">{claim.id}</span>
        <span>
          {holder}, {vehicle}
        </span>
        {next && (
          <button type="button" className="ev-pill sm press next" onClick={() => navigate(`/claims/${next.id}`)}>
            Next claim, {next.index} of {next.total}
          </button>
        )}
      </div>

      <div className="ev-docgrid">
        <LossNotice claim={claim} findings={findings} selectedArea={selectedArea} onOpenPhoto={setViewerId} />
        <aside className="ev-assess" aria-label="Assessment">
          {claim.submitted_at && (
            <ReviewMemo
              claim={claim}
              findings={findings}
              selectedArea={selectedArea}
              onSelectArea={setSelectedArea}
              onUpdate={onUpdate}
            />
          )}
          <DecisionCard claim={claim} findings={findings} onUpdate={onUpdate} />
          <NotesCard claim={claim} onUpdate={onUpdate} />
          <ReportFilesCard claim={claim} onUpdate={onUpdate} />
          <MoreActionsCard claim={claim} onUpdate={onUpdate} />
        </aside>
      </div>

      {viewerId && photos.some((p) => p.item.id === viewerId) && (
        <PhotoViewer
          photos={photos}
          damagesByItem={damagesByItem}
          initialId={viewerId}
          onClose={() => setViewerId(null)}
        />
      )}
    </section>
  )
}

function damagesForItem(claim: ClaimDetail, itemId: string) {
  return (claim.damage?.damages ?? []).filter((d) => d.item_id === itemId)
}

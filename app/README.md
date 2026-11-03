# Claimant link and review console (frontend)

React + TypeScript + Vite single-page app, served by the FastAPI server in `../server`.

- Claimant capture flow (opened from an SMS link): `src/pages/Capture.tsx` and `src/pages/capture/`
- Insurer console (review desk, settings, System health): `src/pages/`, `src/components/`
- API client: `src/api.ts`

```bash
npm ci
npm run dev     # dev server
npm run lint    # oxlint
npm run build   # type-check and build into dist/ (the server serves dist/)
```

See the repository `README.md` for how the whole system runs.

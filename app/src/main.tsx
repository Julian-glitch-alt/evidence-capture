import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import './index.css'
import Capture from './pages/Capture.tsx'
import ChangePassword from './pages/ChangePassword.tsx'
import ClaimList from './pages/ClaimList.tsx'
import ClaimView from './pages/ClaimView.tsx'
import Developers from './pages/Developers.tsx'
import Login from './pages/Login.tsx'
import Privacy from './pages/Privacy.tsx'
import Verify from './pages/Verify.tsx'
import Settings from './pages/claim/Settings.tsx'
import SystemHealth from './pages/SystemHealth.tsx'
import ClaimPage from './pages/demo/ClaimPage.tsx'
import Desktop from './pages/demo/Desktop.tsx'
import NewClaim from './pages/demo/NewClaim.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<ClaimList />} />
        <Route path="/claims/:claimId" element={<ClaimView />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/system" element={<SystemHealth />} />
        <Route path="/c/:token" element={<Capture />} />
        <Route path="/login" element={<Login />} />
        <Route path="/change-password" element={<ChangePassword />} />
        <Route path="/verify" element={<Verify />} />
        <Route path="/privacy" element={<Privacy />} />
        <Route path="/developers" element={<Developers />} />
        <Route path="/demo-insurer" element={<Desktop />} />
        <Route path="/demo-insurer/new" element={<NewClaim />} />
        <Route path="/demo-insurer/claims/:claimNumber" element={<ClaimPage />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)

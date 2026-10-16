import './storageMigration'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { registerServiceWorker } from './pwa/register'
import './index.css'
import './vexa.css'
import { Notifications } from './components/Notifications'
import App from './App.tsx'
import { ErrorBoundary } from './ErrorBoundary'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <Notifications />
      <App />
    </ErrorBoundary>
  </StrictMode>,
)

// Installable on a phone, and repeat loads serve the immutable hashed assets
// from cache. index.html is deliberately never cached — see pwa/serviceWorkerTemplate.ts.
registerServiceWorker()

import { useEffect, useState } from 'react'

type ReadyResponse = {
  status: 'ready' | 'degraded'
  components?: Record<string, string>
} | null

export default function App() {
  const [ready, setReady] = useState<ReadyResponse>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // Same-origin: Nginx strips /api and forwards to backend.
    // No hardcoded localhost:8000 / 127.0.0.1:8000 / EC2 IP anywhere.
    fetch('/api/health/ready')
      .then(async (r) => {
        const body = await r.json()
        setReady(body)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <main className="min-h-full flex flex-col items-center justify-center px-6 text-center">
      <h1 className="text-4xl md:text-5xl font-bold tracking-tight">
        AI Cloud Cost Detective
      </h1>
      <p className="mt-2 text-lg md:text-xl text-slate-300">AWS Edition</p>

      <section className="mt-10 w-full max-w-xl rounded-2xl border border-slate-800 bg-slate-900/60 p-6">
        <h2 className="text-xl font-semibold">Platform Foundation Status</h2>
        <p className="mt-2 text-sm text-slate-400">
          Phase 0: backend, database, and LiteLLM Gateway reachability.
        </p>

        <div className="mt-5 grid gap-2 text-left text-sm">
          <Row k="backend"  v={ready?.components?.backend} />
          <Row k="database" v={ready?.components?.database} />
          <Row k="litellm"  v={ready?.components?.litellm} />
        </div>

        <p className="mt-5 text-xs text-slate-500">
          status: <code className="text-slate-300">{ready?.status ?? (error ? 'error' : 'loading')}</code>
        </p>
      </section>

      <footer className="mt-12 text-xs text-slate-500">
        Phase 0 does not analyze AWS resources yet. Phase 0 does not invoke any LLM.
      </footer>
    </main>
  )
}

function Row({ k, v }: { k: string; v?: string }) {
  const tone =
    v === 'ok'    ? 'text-emerald-400' :
    v === 'error' ? 'text-amber-400'   :
                    'text-slate-400'
  return (
    <div className="flex justify-between border-b border-slate-800 py-1">
      <span className="text-slate-400">{k}</span>
      <span className={tone}>{v ?? '—'}</span>
    </div>
  )
}

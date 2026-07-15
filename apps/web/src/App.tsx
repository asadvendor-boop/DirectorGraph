import { FormEvent, useEffect, useMemo, useState } from 'react'
import { api } from './api'
import type { Project, ProjectBrief, PublicConfig, Shot, TaskRead } from './types'

function webpSource(url?: string | null) {
  return url && url.endsWith('.png') ? url.replace(/\.png$/, '.webp') : null
}

/** Merge a freshly fetched list over local state WITHOUT dropping projects the
 *  server list predates — a judge-test run queued while the background listing
 *  was still in flight would otherwise vanish, bouncing the operator back to the
 *  public demo and re-enabling the (paid) Start test button. */
function mergeProjects(incoming: Project[], current: Project[]): Project[] {
  const known = new Set(incoming.map(item => item.id))
  return [...incoming, ...current.filter(item => !known.has(item.id))]
}

function continuitySummary(continuity: Shot['contract']['continuity']): string {
  if (continuity.required_props.length) return continuity.required_props.join(', ')
  const wardrobe = Object.values(continuity.wardrobe ?? {}).filter(Boolean)
  const parts = [continuity.location, continuity.time_of_day, wardrobe.length ? `wardrobe locked: ${wardrobe.join(', ')}` : '']
  const summary = parts.filter(Boolean).join(' · ')
  return summary || 'Environment continuity only'
}


const ACTIVE = new Set(['queued', 'planning', 'storyboarding', 'producing', 'inspecting', 'editing'])
const ACTIVE_TASK = new Set(['pending', 'running'])
const TASK_STORAGE_KEY = 'directorgraph.activeTask'
const STAGES = ['planning', 'storyboarding', 'producing', 'inspecting', 'editing', 'completed'] as const
const initialBrief: ProjectBrief = {
  title: 'The Last Delivery',
  premise: 'Every night a small courier robot leaves a package at the same apartment. On its final night before decommissioning, the door finally opens.',
  genre: 'science-fiction drama',
  tone: 'cinematic, intimate, emotionally resonant',
  target_audience: 'global short-form drama viewers',
  duration_seconds: 42,
  aspect_ratio: '9:16',
  language: 'English',
  visual_style: 'grounded cinematic realism, controlled lighting, shallow depth of field',
  budget_usd: 20,
  repair_reserve_percent: 18,
  seed: 20260710,
  required_prop: 'red paper crane'
}

type Tab = 'film' | 'story' | 'shots' | 'evidence'

function money(value: number) { return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value) }
function pct(value: number) { return `${Math.round(value * 100)}%` }
function titleCase(value: string) { return value.replaceAll('_', ' ').replace(/\b\w/g, char => char.toUpperCase()) }
function time(value: string) { return new Intl.DateTimeFormat('en', { hour: '2-digit', minute: '2-digit' }).format(new Date(value)) }
function shortId(value: string) { return value.length > 24 ? `${value.slice(0, 21)}…` : value }

function storedTask(): { project_id: string; task_id: string } | null {
  try {
    const value = window.localStorage.getItem(TASK_STORAGE_KEY)
    if (!value) return null
    const parsed = JSON.parse(value) as { project_id?: string; task_id?: string }
    return parsed.project_id && parsed.task_id ? { project_id: parsed.project_id, task_id: parsed.task_id } : null
  } catch {
    return null
  }
}

function storeTask(task: TaskRead | { project_id: string; task_id: string } | null) {
  if (!task) {
    window.localStorage.removeItem(TASK_STORAGE_KEY)
    return
  }
  window.localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify({ project_id: task.project_id, task_id: task.task_id }))
}

function Mark() {
  return <div className="brand-mark" aria-hidden="true"><img src="/brand/dg-icon-128.png" alt="" /></div>
}

const AGENT_TONES = ['amber', 'blue', 'green', 'violet', 'red'] as const
function agentTone(agent: string) {
  let hash = 0
  for (let i = 0; i < agent.length; i += 1) hash = (hash * 31 + agent.charCodeAt(i)) % 997
  return AGENT_TONES[hash % AGENT_TONES.length]
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status}`}><i />{titleCase(status)}</span>
}

function TaskChip({ task, onStop, stopping }: { task: TaskRead; onStop?: () => void; stopping?: boolean }) {
  const durable = task.durable_status
  return <div className={`task-chip task-${task.status}`}>
    <small>{durable?.dispatch_mode ?? task.type}</small>
    <b>{titleCase(task.status)}</b>
    <span>{shortId(task.task_id)}</span>
    {onStop ? <button type="button" onClick={onStop} disabled={stopping}>{stopping ? 'Stopping' : 'Stop'}</button> : null}
  </div>
}

function Metric({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article className="metric-card"><div className="metric-label">{label}</div><strong>{value}</strong><p>{detail}</p></article>
}

function ProductionRail({ project }: { project: Project }) {
  const index = project.status === 'draft' ? -1 : STAGES.indexOf(project.status as typeof STAGES[number])
  return <section className="production-rail" aria-label="Production stages">
    {STAGES.map((stage, i) => {
      const done = project.status === 'completed' || i < index
      const active = i === index
      return <div className={`rail-step ${done ? 'done' : ''} ${active ? 'active' : ''}`} key={stage}>
        <span>{done ? '✓' : i + 1}</span><div><b>{titleCase(stage)}</b><small>{stage === 'planning' ? 'StoryIR' : stage === 'storyboarding' ? 'References' : stage === 'producing' ? 'Wan / HappyHorse' : stage === 'inspecting' ? 'Qwen-VL QC' : stage === 'editing' ? 'FFmpeg master' : 'Ready'}</small></div>
      </div>
    })}
  </section>
}

function ShotCard({ shot, onSelect }: { shot: Shot; onSelect: (shot: Shot) => void }) {
  return <button className="shot-card" onClick={() => onSelect(shot)}>
    <div className="shot-visual">
      {shot.storyboard_url ? <picture>{webpSource(shot.storyboard_url) && <source srcSet={webpSource(shot.storyboard_url)!} type="image/webp" />}<img src={shot.storyboard_url} alt={`${shot.shot_code} storyboard`} loading="lazy" decoding="async" /></picture> : <div className="shot-placeholder">{shot.shot_code}</div>}
      <span className="shot-duration">{shot.contract.duration_seconds}s</span>
      <span className={`shot-state shot-${shot.status}`}>{titleCase(shot.status)}</span>
    </div>
    <div className="shot-copy">
      <div><span>{shot.shot_code}</span><b>{shot.contract.title}</b></div>
      <p>{shot.contract.narrative_objective}</p>
      <footer><span>{shot.contract.renderer}</span><span>{shot.contract.resolution}</span><span>Salience {shot.contract.salience.toFixed(2)}</span></footer>
    </div>
  </button>
}

function ShotInspector({ shot, close }: { shot: Shot; close: () => void }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={close}>
    <section className="inspector" role="dialog" aria-modal="true" aria-label={`${shot.shot_code} details`} onMouseDown={event => event.stopPropagation()}>
      <header><div><small>SHOT CONTRACT</small><h2>{shot.shot_code} · {shot.contract.title}</h2></div><button className="icon-button" onClick={close}>×</button></header>
      <div className="inspector-grid">
        <div>
          {shot.video_url ? <video src={shot.video_url} controls preload="metadata" poster={shot.storyboard_url ? (webpSource(shot.storyboard_url) ?? shot.storyboard_url) : undefined} /> : shot.storyboard_url ? <picture>{webpSource(shot.storyboard_url) && <source srcSet={webpSource(shot.storyboard_url)!} type="image/webp" />}<img src={shot.storyboard_url} alt="Storyboard" loading="lazy" decoding="async" /></picture> : null}
          <div className="contract-grid">
            <div><small>OBJECTIVE</small><p>{shot.contract.narrative_objective}</p></div>
            <div><small>ACTION</small><p>{shot.contract.action}</p></div>
            <div><small>CAMERA</small><p>{shot.contract.camera.framing}; {shot.contract.camera.movement}; {shot.contract.camera.angle}</p></div>
            <div><small>CONTINUITY</small><p>{continuitySummary(shot.contract.continuity)}</p></div>
          </div>
        </div>
        <aside>
          <div className="inspector-score"><small>QC SCORE</small><strong>{shot.quality ? pct(shot.quality.overall_score) : '—'}</strong><StatusPill status={shot.status} /></div>
          {shot.quality?.dimensions.map(dimension => <div className="dimension" key={dimension.name}>
            <div><span>{titleCase(dimension.name)}</span><b>{pct(dimension.score)}</b></div>
            <div className="bar"><i style={{ width: pct(dimension.score) }} /></div>
            <p>{dimension.evidence}</p>
          </div>)}
          {!shot.quality ? <p className="inspector-waiting">Qwen-VL inspection runs after this shot renders; the contract facts below are already locked.</p> : null}
          {shot.quality && !shot.quality.dimensions.length ? <p className="inspector-waiting">This sealed verdict recorded a single overall score. New inspections also seal the per-dimension breakdown.</p> : null}
          {shot.quality?.violations.length ? <div className="violation"><b>Repair rationale</b><p>{shot.quality.violations.join(' ')}</p><small>{shot.quality.repair_instruction}</small></div> : null}
          <div className="inspector-facts">
            <div><small>RENDERER</small><b>{shot.contract.renderer}</b></div>
            <div><small>RESOLUTION</small><b>{shot.contract.resolution}</b></div>
            <div><small>SALIENCE</small><b>{shot.contract.salience.toFixed(2)}</b></div>
            <div><small>QC THRESHOLD</small><b>{pct(shot.contract.quality_threshold)}</b></div>
            <div><small>ATTEMPTS</small><b>{shot.attempts} / {shot.contract.max_retries + 1}</b></div>
            <div><small>EVALUATOR</small><b>{shot.quality?.evaluator_model ?? 'Qwen-VL (armed)'}</b></div>
          </div>
          {shot.contract.narration || shot.contract.dialogue ? <div className="inspector-voice"><small>{shot.contract.narration ? 'NARRATION' : 'DIALOGUE'}</small><p>{shot.contract.narration ?? shot.contract.dialogue}</p></div> : null}
          {shot.audio_url ? <div className="inspector-voice"><small>SHOT AUDIO</small><audio src={shot.audio_url} controls preload="none" /></div> : null}
        </aside>
      </div>
    </section>
  </div>
}

function CreateModal({ close, created }: { close: () => void; created: (project: Project) => void }) {
  const [brief, setBrief] = useState(initialBrief)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const update = <K extends keyof ProjectBrief>(key: K, value: ProjectBrief[K]) => setBrief(current => ({ ...current, [key]: value }))
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { created(await api.create(brief)) } catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not create project') } finally { setBusy(false) }
  }
  return <div className="modal-backdrop" onMouseDown={close}>
    <form className="create-modal" onSubmit={submit} onMouseDown={event => event.stopPropagation()}>
      <header><div><small>NEW PRODUCTION</small><h2>Compile a drama from one brief</h2></div><button type="button" className="icon-button" onClick={close}>×</button></header>
      <label>Project title<input value={brief.title} onChange={e => update('title', e.target.value)} required minLength={2} /></label>
      <label className="wide">Premise<textarea value={brief.premise} onChange={e => update('premise', e.target.value)} required minLength={12} rows={4} /></label>
      <label>Genre<input value={brief.genre} onChange={e => update('genre', e.target.value)} /></label>
      <label>Tone<input value={brief.tone} onChange={e => update('tone', e.target.value)} /></label>
      <label>Duration<select value={brief.duration_seconds} onChange={e => update('duration_seconds', Number(e.target.value))}><option value={21}>21 seconds</option><option value={28}>28 seconds</option><option value={42}>42 seconds</option><option value={60}>60 seconds</option></select></label>
      <label>Aspect ratio<select value={brief.aspect_ratio} onChange={e => update('aspect_ratio', e.target.value as ProjectBrief['aspect_ratio'])}><option>9:16</option><option>16:9</option><option>1:1</option></select></label>
      <label>Budget<input type="number" min="1" max="1000" step="1" value={brief.budget_usd} onChange={e => update('budget_usd', Number(e.target.value))} /></label>
      <label>Repair reserve<input type="number" min="5" max="40" value={brief.repair_reserve_percent} onChange={e => update('repair_reserve_percent', Number(e.target.value))} /></label>
      <label className="wide">Visual language<input value={brief.visual_style} onChange={e => update('visual_style', e.target.value)} /></label>
      <label className="wide">Continuity anchor<input value={brief.required_prop ?? ''} onChange={e => update('required_prop', e.target.value)} /></label>
      {error ? <p className="form-error wide">{error}</p> : null}
      <footer className="wide"><button type="button" className="button ghost" onClick={close}>Cancel</button><button className="button primary" disabled={busy}>{busy ? 'Compiling brief…' : 'Create production'}</button></footer>
    </form>
  </div>
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [config, setConfig] = useState<PublicConfig | null>(null)
  const [tab, setTab] = useState<Tab>('film')
  const [createOpen, setCreateOpen] = useState(false)
  const [shotOpen, setShotOpen] = useState<Shot | null>(null)
  const [publicDemoId, setPublicDemoId] = useState<string | null>(null)
  const [activeTask, setActiveTask] = useState<TaskRead | null>(null)
  const [judgeCode, setJudgeCode] = useState('')
  const [patch, setPatch] = useState('Make the final dawn warmer and let Courier-7’s light pulse twice.')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [stoppingTask, setStoppingTask] = useState(false)
  const [booting, setBooting] = useState(true)

  const project = projects.find(item => item.id === selectedId) ?? projects[0] ?? null
  const readOnlyDemo = Boolean(project && publicDemoId === project.id)
  const projectTask = project && activeTask?.project_id === project.id ? activeTask : null
  const taskActive = Boolean(projectTask && ACTIVE_TASK.has(projectTask.status))
  // Guards the PAID judge-test button: once any run is queued it stays disabled
  // even if the selected project changes underneath us.
  const anyTaskActive = Boolean(activeTask && ACTIVE_TASK.has(activeTask.status))
  const visibleProjects = useMemo(() => {
    if (!publicDemoId) return projects
    return projects.filter(item => item.id === publicDemoId || item.id === activeTask?.project_id)
  }, [projects, publicDemoId, activeTask?.project_id])
  const refreshList = async () => {
    const data = await api.projects()
    setProjects(current => mergeProjects(data, current))
    if (!selectedId && data[0]) setSelectedId(data[0].id)
  }
  useEffect(() => {
    async function load() {
      const cfg = await api.config()
      setConfig(cfg)
      // Load the public demo first so the judged production renders
      // immediately; the full project listing follows in the background.
      let demo: Project | null = null
      if (cfg.public_demo_project_id) {
        try {
          demo = await api.publicDemo()
          setPublicDemoId(demo.id)
          setProjects([demo])
          setSelectedId(demo.id)
          setBooting(false)
        } catch {
          setPublicDemoId(cfg.public_demo_project_id)
        }
      }
      const data = await api.projects()
      const merged = demo ? [demo, ...data.filter(item => item.id !== demo!.id)] : data
      setProjects(current => mergeProjects(merged, current))
      if (!cfg.public_demo_project_id && !selectedId && merged[0]) setSelectedId(merged[0].id)
    }
    load().catch(cause => setError(cause.message)).finally(() => setBooting(false))
  }, [])
  useEffect(() => {
    // Restore a stored task on mount, regardless of which project is selected.
    // Requiring the task's project to be selected first deadlocked after a reload:
    // the sidebar hides non-demo projects until the task is restored, so the judge
    // could never select their own run again while it kept spending.
    const stored = storedTask()
    if (!stored) return
    api.task(stored.task_id)
      .then(task => {
        setActiveTask(task)
        if (!ACTIVE_TASK.has(task.status)) storeTask(null)
      })
      .catch(() => storeTask(null))
  }, [])
  useEffect(() => {
    if (!project || !ACTIVE.has(project.status)) return
    const id = window.setInterval(async () => {
      try {
        const updated = await api.project(project.id)
        setProjects(current => current.map(item => item.id === updated.id ? updated : item))
      } catch (cause) { setError(cause instanceof Error ? cause.message : 'Refresh failed') }
    }, 1500)
    return () => window.clearInterval(id)
  }, [project?.id, project?.status])
  useEffect(() => {
    if (!projectTask || !ACTIVE_TASK.has(projectTask.status)) return
    const poll = async () => {
      try {
        const [task, updated] = await Promise.all([
          api.task(projectTask.task_id),
          api.project(projectTask.project_id)
        ])
        setActiveTask(task)
        setProjects(current => current.map(item => item.id === updated.id ? updated : item))
        if (!ACTIVE_TASK.has(task.status)) storeTask(null)
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : 'Task refresh failed')
      }
    }
    const id = window.setInterval(poll, 1500)
    return () => window.clearInterval(id)
  }, [projectTask?.task_id, projectTask?.status])

  const repairedShot = useMemo(() => project?.shots.find(shot => shot.attempts > 1), [project])
  const rejectedEvent = useMemo(() => project?.events.find(event => event.kind === 'shot.rejected' && event.payload.shot === repairedShot?.shot_code), [project, repairedShot])
  const avoidedSeconds = project && repairedShot ? Math.max(project.brief.duration_seconds - repairedShot.contract.duration_seconds, 0) : 0
  const acceptedQualityScore = useMemo(() => {
    if (!project?.shots.length) return null
    const scored = project.shots.filter(shot => shot.quality)
    if (!scored.length) return null
    return scored.reduce((sum, shot) => sum + (shot.quality?.overall_score ?? 0), 0) / scored.length
  }, [project])
  const firstPassScore = rejectedEvent ? Number(rejectedEvent.payload.score) : acceptedQualityScore
  const acceptedCutScore = repairedShot?.quality?.overall_score ?? acceptedQualityScore
  const repairCount = project ? project.ledger.local_repairs + project.ledger.full_regenerations : 0
  const acceptedShots = project?.shots.filter(shot => shot.accepted).length ?? 0
  const inspectedShots = project?.shots.filter(shot => shot.quality).length ?? 0
  const rendererMix = useMemo(() => {
    if (!project?.shots.length) return 'Awaiting shot contracts'
    const counts = project.shots.reduce<Record<string, number>>((acc, shot) => {
      acc[shot.contract.renderer] = (acc[shot.contract.renderer] ?? 0) + 1
      return acc
    }, {})
    return Object.entries(counts).map(([name, count]) => `${name}: ${count}`).join(' · ')
  }, [project])
  const usesFallbackProvider = useMemo(() => {
    // Read the structured degradation signals the backend records, not a regex over
    // stringified payloads: `degraded` is set by the story planner's fallback path,
    // and provider fallbacks stamp "+local-fallback:<reason>" into the model fields.
    if (!project) return false
    const fallbackModel = (value: unknown) => typeof value === 'string' && value.includes('local-fallback')
    return project.events.some(event => (event.payload as Record<string, unknown> | null)?.degraded === true)
      || project.events.some(event => fallbackModel((event.payload as Record<string, unknown> | null)?.model))
      || project.shots.some(shot => fallbackModel(shot.quality?.evaluator_model))
  }, [project])

  async function run() {
    if (!project) return
    setBusy(true); setError('')
    try {
      const submitted = await api.run(project.id)
      const [task, updated] = await Promise.all([api.task(submitted.task_id), api.project(project.id)])
      setActiveTask(task)
      storeTask(task)
      setProjects(current => current.map(item => item.id === updated.id ? updated : item))
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Production could not start') }
    finally { setBusy(false) }
  }
  async function applyPatch() {
    if (!project || !patch.trim()) return
    setBusy(true); setError('')
    try {
      const submitted = await api.patch(project.id, patch)
      const [task, updated] = await Promise.all([api.task(submitted.task_id), api.project(project.id)])
      setActiveTask(task)
      storeTask(task)
      setProjects(current => current.map(item => item.id === updated.id ? updated : item))
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Patch could not start') }
    finally { setBusy(false) }
  }
  async function startJudgeTest() {
    setBusy(true); setError('')
    try {
      const submitted = await api.judgeTest(judgeCode)
      const [task, created] = await Promise.all([api.task(submitted.task_id), api.project(submitted.project_id)])
      setProjects(current => [created, ...current.filter(item => item.id !== created.id)])
      setSelectedId(created.id)
      setActiveTask(task)
      storeTask(task)
      setTab('film')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Judge Test could not start')
    } finally {
      setBusy(false)
    }
  }
  async function stopTask() {
    if (!projectTask) return
    setStoppingTask(true); setError('')
    try {
      const stopped = await api.stopTask(projectTask.task_id, judgeCode)
      const updated = await api.project(projectTask.project_id)
      setActiveTask(stopped)
      storeTask(null)
      setProjects(current => current.map(item => item.id === updated.id ? updated : item))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Task could not be stopped')
    } finally {
      setStoppingTask(false)
    }
  }

  return <div className="app-shell">
    <header className="topbar">
      <div className="brand"><Mark /><div><b>DirectorGraph</b><span>Self-correcting AI showrunner</span></div></div>
      <div className="cloud-state"><span className="pulse" /><div><small>STUDIO MODE</small><b>{config ? (config.provider_mode === 'live' ? 'Qwen Cloud · Live' : 'Local proof studio') : 'Connecting…'}</b></div></div>
      {!publicDemoId ? <button className="button primary compact" onClick={() => setCreateOpen(true)}>+ New production</button> : null}
    </header>

    <aside className="sidebar">
      <div className="side-title"><small>PRODUCTIONS</small><span>{visibleProjects.length}</span></div>
      <nav>
        {visibleProjects.map(item => {
          const poster = item.shots?.find(shot => shot.storyboard_url)?.storyboard_url
          return <button className={item.id === project?.id ? 'selected' : ''} key={item.id} onClick={() => { setSelectedId(item.id); setTab('film') }}>
            <div className="project-thumb">{poster ? <picture>{webpSource(poster) && <source srcSet={webpSource(poster)!} type="image/webp" />}<img src={poster} alt="" loading="lazy" decoding="async" /></picture> : <span>{item.title.slice(0, 1)}</span>}</div>
            <div><b>{item.title}</b><small>{item.brief.duration_seconds}s · {item.brief.aspect_ratio}</small></div>
            <i className={`project-dot dot-${item.status}`} />
          </button>
        })}
      </nav>
      <footer><b>Built on Qwen Cloud</b><span>{publicDemoId ? 'Read-only public demo' : 'Story · Vision · Video · Voice'}</span></footer>
    </aside>

    <main>
      {error ? <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div> : null}
      {booting && !project ? <section className="boot-screen" aria-label="Loading production">
        <div className="skeleton skeleton-title" />
        <div className="skeleton skeleton-rail" />
        <div className="skeleton-row"><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></div>
        <div className="skeleton skeleton-stage" />
      </section> : !project ? <section className="empty"><Mark /><h1>Your autonomous studio is ready.</h1><p>Create a brief. DirectorGraph will write, storyboard, render, inspect, repair, and edit the drama.</p><button className="button primary" onClick={() => setCreateOpen(true)}>Create the first production</button></section> : <>
        <section className="project-header">
          <div><div className="eyebrow"><StatusPill status={project.status} /><span>TRACK 2 · AI SHOWRUNNER</span></div><h1>{project.title}</h1><p>{project.plan?.logline ?? project.brief.premise}</p></div>
          <div className="header-actions">
            {projectTask ? <TaskChip task={projectTask} onStop={projectTask.status === 'pending' ? stopTask : undefined} stopping={stoppingTask} /> : null}
            {!readOnlyDemo && (project.status === 'draft' || project.status === 'failed') ? <button className="button primary" onClick={run} disabled={busy || taskActive}>{busy ? 'Queuing…' : '▶ Run autonomous production'}</button> : null}
            {project.status === 'completed' ? <a className="button ghost" href={readOnlyDemo ? api.publicDemoManifestUrl() : api.manifestUrl(project.id)} target="_blank" rel="noreferrer">Open manifest ↗</a> : null}
          </div>
        </section>

        <ProductionRail project={project} />

        <section className="metrics">
          <Metric label="Mean QC score" value={acceptedQualityScore !== null ? pct(acceptedQualityScore) : '—'} detail={`${project.shots.filter(shot => shot.accepted).length}/${project.shots.length || 0} shots accepted`} />
          <Metric label="Production efficiency" value={project.ledger.video_seconds_generated ? pct(project.ledger.acceptance_ratio) : '—'} detail={`${project.ledger.video_seconds_accepted}s accepted / ${project.ledger.video_seconds_generated}s rendered`} />
          <Metric label="Budget remaining" value={money(project.ledger.budget_remaining_usd)} detail={`${money(project.ledger.estimated_cost_usd)} estimated spend`} />
          <Metric label="Autonomous repairs" value={repairCount ? String(repairCount) : inspectedShots ? 'QC pass' : '—'} detail={repairCount ? `${project.ledger.local_repairs} surgical · ${project.ledger.full_regenerations} full` : inspectedShots ? `${project.shots.filter(shot => shot.accepted).length} shots accepted first pass` : 'Inspection has not run yet'} />
        </section>

        <div className="tabs" role="tablist">
          {(['film', 'story', 'shots', 'evidence'] as Tab[]).map(value => <button role="tab" aria-selected={tab === value} className={tab === value ? 'active' : ''} onClick={() => setTab(value)} key={value}>{titleCase(value)}</button>)}
        </div>

        {tab === 'film' ? <section className="workspace film-stack">
          <section className="film-cockpit">
            <article className="cockpit-primary">
              <small>JUDGE VIEW</small>
              <h2>Judge proof cockpit</h2>
              <p>DirectorGraph is evaluated from the contract trail first: Qwen writes the story graph, Wan and HappyHorse produce media, Qwen-VL inspects every shot, and only accepted outputs reach the master cut.</p>
              <div className="cockpit-tags">
                <span>Contract-first production</span>
                <span>Qwen-VL inspection loop</span>
                <span>Media player is secondary</span>
              </div>
            </article>
            <div className="cockpit-right">
              <div className="cockpit-kpis">
                <div><small>SHOTS ACCEPTED</small><strong>{acceptedShots}/{project.shots.length || 0}</strong><span>{project.ledger.video_seconds_accepted}s accepted media</span></div>
                <div><small>QC SCORE</small><strong>{acceptedQualityScore !== null ? pct(acceptedQualityScore) : '—'}</strong><span>Qwen-VL mean across scored shots</span></div>
                <div><small>REPAIR SIGNAL</small><strong>{repairCount ? `${repairCount} loop` : inspectedShots ? 'First pass' : 'Armed'}</strong><span>{repairedShot ? `${repairedShot.shot_code} repaired` : repairCount ? 'Ledger-recorded repairs' : inspectedShots ? 'No rejection required' : 'Awaiting inspection'}</span></div>
                <div><small>LIVE MODE</small><strong>{config ? (config.provider_mode === 'live' ? 'Qwen Cloud' : 'Local proof') : '—'}</strong><span>{usesFallbackProvider ? 'Fallback disclosed' : 'Provider path clean'}</span></div>
              </div>
              <div className="cockpit-shot-wall" aria-label="Shot contract wall">
                {project.shots.slice(0, 6).map(shot => <button type="button" key={shot.id} className="wall-tile" onClick={() => setShotOpen(shot)} title={`Open ${shot.shot_code} contract`}>
                  {shot.storyboard_url ? <picture>{webpSource(shot.storyboard_url) && <source srcSet={webpSource(shot.storyboard_url)!} type="image/webp" />}<img src={shot.storyboard_url} alt={`${shot.shot_code} storyboard`} loading="lazy" decoding="async" /></picture> : <span className="wall-code">{shot.shot_code}</span>}
                  <i className={`wall-state shot-${shot.status}`}>{titleCase(shot.status)}{shot.attempts ? ` · attempt ${shot.attempts}` : ''}</i>
                  <div className="wall-meta"><span>{shot.shot_code} · {shot.contract.duration_seconds}s</span><b>{shot.quality ? `${pct(shot.quality.overall_score)} VL` : shot.contract.renderer}</b></div>
                </button>)}
                {!project.shots.length ? <div className="wall-empty"><b>Story graph waiting</b><small>Run production to compile shot contracts</small></div> : null}
              </div>
            </div>
          </section>
          <div className="film-workspace">
          <div className="player-panel">
            <div className="panel-heading"><div><small>FINAL MASTER</small><h2>{project.status === 'completed' ? (usesFallbackProvider ? 'Verified fallback cut' : 'Final verified cut') : 'Production preview'}</h2></div><span>{project.brief.aspect_ratio} · {project.brief.duration_seconds}s</span></div>
            <div className={`player-frame ratio-${project.brief.aspect_ratio.replace(':', '-')}`}>
              {project.final_video_url ? <video src={project.final_video_url} controls preload="metadata">{readOnlyDemo ? <track kind="captions" srcLang="en" label="English" src={project.final_video_url.replace(/\.mp4(\?.*)?$/, '.vtt$1')} default /> : null}</video> : <div className="player-empty"><Mark /><b>{titleCase(project.status)}</b><p>The master appears after every shot clears its contract.</p></div>}
            </div>
            {readOnlyDemo && config?.judge_access_code_configured ? <div className="judge-test-box"><div><small>JUDGE TEST</small><b>{config.provider_mode === 'live' ? 'Capped Qwen Cloud run' : 'Capped local proof run'}</b></div><input type="password" value={judgeCode} onChange={e => setJudgeCode(e.target.value)} placeholder="Access code" /><button className="button secondary" onClick={startJudgeTest} disabled={busy || anyTaskActive || !judgeCode.trim()}>{anyTaskActive ? 'Run in progress' : 'Start test'}</button></div> : null}
            {!readOnlyDemo && project.status === 'completed' ? <div className="patch-box"><div><small>SEMANTIC PATCH RENDERING</small><b>Revise the story without rerendering unaffected shots.</b></div><textarea value={patch} onChange={e => setPatch(e.target.value)} rows={2} /><button className="button secondary" onClick={applyPatch} disabled={busy || taskActive}>Analyze impact & patch</button></div> : null}
          </div>
          <aside className="activity-panel">
            <div className="panel-heading"><div><small>AGENT TRACE</small><h2>Production decisions</h2></div><span className="live-label"><i /> LIVE</span></div>
            <div className="event-list">
              {[...project.events].reverse().map(event => <article key={event.id} className={`tone-${agentTone(event.agent)}`}><div className="event-node" /><div className="event-body"><header><b>{event.agent}</b><time>{time(event.created_at)}</time></header><p>{event.message}</p><small className="event-kind">{event.kind}{typeof event.payload?.score === 'number' ? ` · score ${Number(event.payload.score).toFixed(2)}` : ''}</small></div></article>)}
              {!project.events.length ? <p className="muted">No production events yet.</p> : null}
            </div>
          </aside>
          </div>
        </section> : null}

        {tab === 'story' ? <section className="workspace story-workspace">
          <article className="story-bible"><small>STORY BIBLE</small><h2>Story map</h2><h3>{project.plan?.theme ?? 'Awaiting StoryIR'}</h3><p>{project.plan?.synopsis ?? 'Run production to compile the brief into a typed narrative graph.'}</p><div className="rule-columns"><div><b>Locked continuity rules</b>{project.plan?.visual_rules.map(rule => <span key={rule}>✓ {rule}</span>)}</div><div><b>Audio invariants</b>{project.plan?.audio_rules.map(rule => <span key={rule}>✓ {rule}</span>)}</div></div></article>
          <article className="beat-graph"><small>DRAMATIC BEAT GRAPH</small><h2>Beat rail</h2><div>{project.plan?.beats.map((beat, i) => {
            const beatShot = project.shots.find(shot => shot.contract.beat_id === beat.id && shot.storyboard_url)
            return <div className="beat" key={beat.id}><span>{i + 1}</span><section><div className="beat-head"><em className={`arc-chip arc-${beat.beat_type.toLowerCase().replace(/[^a-z]+/g, '-')}`}>{titleCase(beat.beat_type)}</em><small>{beat.duration_seconds}s</small></div><b>{beat.name}</b><p>{beat.objective}</p><em className="beat-shift">{beat.emotional_shift}</em></section>{beatShot?.storyboard_url ? <picture className="beat-thumb-wrap">{webpSource(beatShot.storyboard_url) && <source srcSet={webpSource(beatShot.storyboard_url)!} type="image/webp" />}<img className="beat-thumb" src={beatShot.storyboard_url} alt={`${beat.name} storyboard`} loading="lazy" decoding="async" /></picture> : <span className="beat-thumb beat-thumb-empty">{beat.beat_type.slice(0, 1).toUpperCase()}</span>}</div>
          }) ?? <p className="muted">No beats compiled.</p>}</div></article>
          <article className="cast"><small>LOCKED CHARACTERS</small>{project.plan?.characters.map(character => <div key={character.id}>{character.reference_url ? <picture>{webpSource(character.reference_url) && <source srcSet={webpSource(character.reference_url)!} type="image/webp" />}<img src={character.reference_url} alt={`${character.name} locked reference`} loading="lazy" decoding="async" /></picture> : <span>{character.name.slice(0, 1)}</span>}<section><b>{character.name}</b><small>{character.role}</small><p>{character.motivation || character.appearance}</p><div className="wardrobe-tags">{character.wardrobe.split(/[,;]/).map(tag => tag.trim()).filter(Boolean).slice(0, 4).map(tag => <span key={tag}>{tag}</span>)}</div></section></div>)}</article>
        </section> : null}

        {tab === 'shots' ? <section className="workspace shots-workspace">
          <section className="shots-overview">
            <div><small>SHOT SURFACE</small><h2>Shot contract board</h2><p>Every card is a typed production contract: objective, renderer, duration, salience, threshold, and continuity anchor.</p></div>
            <aside><small>Renderer mix</small><strong>{rendererMix}</strong><span>{project.brief.aspect_ratio} · {project.brief.duration_seconds}s target</span></aside>
          </section>
          <section className="shot-grid">{project.shots.length ? project.shots.map(shot => <ShotCard key={shot.id} shot={shot} onSelect={setShotOpen} />) : <div className="empty-grid">Shot contracts appear after narrative compilation.</div>}</section>
        </section> : null}

        {tab === 'evidence' ? <section className="workspace evidence-workspace">
          <article className="repair-proof"><small>QUALITY LOOP EVIDENCE</small><h2>Evidence ledger</h2><h3>{repairedShot ? `${repairedShot.shot_code} repaired autonomously` : inspectedShots ? 'Every shot cleared inspection first pass' : 'Inspection armed'}</h3>{rejectedEvent ? <div className="score-compare"><div><span>FIRST PASS</span><strong>{firstPassScore !== null && Number.isFinite(firstPassScore) ? pct(firstPassScore) : 'Inspection armed'}</strong></div><i>→</i><div><span>ACCEPTED CUT</span><strong>{acceptedCutScore !== null && Number.isFinite(acceptedCutScore) ? pct(acceptedCutScore) : 'Pending QC'}</strong></div></div> : <div className="score-compare"><div><span>ACCEPTED CUT</span><strong>{acceptedCutScore !== null && Number.isFinite(acceptedCutScore) ? pct(acceptedCutScore) : 'Pending QC'}</strong></div></div>}<p>{repairedShot?.quality?.dimensions.find(item => item.name === 'continuity')?.evidence ?? project.shots.find(shot => shot.quality)?.quality?.dimensions.find(item => item.name === 'continuity')?.evidence ?? 'The Continuity Supervisor compares every clip with a typed Shot Contract.'}</p></article>
          <article className="efficiency-proof"><small>{repairedShot ? 'MINIMUM-COST REPAIR' : 'INSPECTION PATH'}</small><h2>{repairedShot ? `${avoidedSeconds}s` : `${project.ledger.video_seconds_accepted}s accepted`}</h2><p>{repairedShot ? `Estimated extra generation avoided by repairing only ${repairedShot.shot_code} instead of rerendering the entire ${project.brief.duration_seconds}-second film.` : inspectedShots ? 'No repair was required in this run; every generated second cleared Qwen-VL inspection on the accepted path.' : 'Inspection has not run yet; accepted seconds accrue once shots clear their contracts.'}</p><div className="bar large"><i style={{ width: repairedShot && project.brief.duration_seconds ? pct(avoidedSeconds / project.brief.duration_seconds) : pct(project.ledger.acceptance_ratio || 0) }} /></div></article>
          <article className="token-ledger"><small>Budget and repair accounting</small><dl><div><dt>Text tokens</dt><dd>{(project.ledger.text_input_tokens + project.ledger.text_output_tokens).toLocaleString()}</dd></div><div><dt>Vision tokens</dt><dd>{project.ledger.vision_input_tokens.toLocaleString()}</dd></div><div><dt>Generated seconds</dt><dd>{project.ledger.video_seconds_generated}s</dd></div><div><dt>Rejected seconds</dt><dd>{project.ledger.rejected_generation_seconds ? `${project.ledger.rejected_generation_seconds}s` : 'None required'}</dd></div><div><dt>Storyboards</dt><dd>{project.ledger.image_count}</dd></div><div><dt>Repair reserve</dt><dd>{money(project.ledger.repair_reserve_usd)}</dd></div></dl></article>
          <article className="architecture-card"><small>PRODUCTION-GRADE CONTROLS</small><ul><li><b>Typed contracts</b><span>StoryIR validates every beat and shot dependency.</span></li><li><b>Durable jobs</b><span>Idempotency keys and stale-lock recovery prevent duplicate productions.</span></li><li><b>Model router</b><span>Character-bound shots route to Wan r2v; salience sets resolution and retries.</span></li><li><b>Asset audit_trail</b><span>The manifest records prompts, attempts, scores, and output URLs.</span></li></ul></article>
        </section> : null}
      </>}
    </main>
    {createOpen && !publicDemoId ? <CreateModal close={() => setCreateOpen(false)} created={created => { setProjects(current => [created, ...current]); setSelectedId(created.id); setCreateOpen(false) }} /> : null}
    {shotOpen ? <ShotInspector shot={shotOpen} close={() => setShotOpen(null)} /> : null}
  </div>
}

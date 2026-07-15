import { expect, test, type Page, type Route } from '@playwright/test'

const now = '2026-06-24T15:00:00.000Z'

function ledger() {
  return {
    text_input_tokens: 1200,
    text_output_tokens: 900,
    vision_input_tokens: 600,
    video_seconds_generated: 24,
    video_seconds_accepted: 21,
    rejected_generation_seconds: 3,
    image_count: 4,
    full_regenerations: 0,
    local_repairs: 1,
    estimated_cost_usd: 2.4,
    budget_usd: 6,
    repair_reserve_usd: 1.2,
    budget_remaining_usd: 3.6,
    acceptance_ratio: 0.875
  }
}

function shot() {
  return {
    id: 'shot-s05',
    shot_code: 'S05',
    sequence: 5,
    status: 'accepted',
    storyboard_url: null,
    audio_url: null,
    video_url: null,
    attempts: 2,
    accepted: true,
    contract: {
      id: 'contract-s05',
      sequence: 5,
      beat_id: 'beat-3',
      title: 'The door opens',
      duration_seconds: 3,
      aspect_ratio: '9:16',
      narrative_objective: 'Reveal the red paper crane without losing continuity.',
      characters: ['Courier-7', 'Mira'],
      action: 'Courier-7 projects a final message as Mira picks up the crane.',
      dialogue: null,
      narration: 'The final delivery is received.',
      emotion: 'bittersweet relief',
      location: 'Apartment hallway',
      camera: { framing: 'medium close-up', movement: 'slow push', angle: 'eye-level', lens: '50mm' },
      continuity: {
        location: 'Apartment hallway',
        time_of_day: 'pre-dawn',
        wardrobe: { Mira: 'blue coat' },
        required_props: ['red paper crane'],
        start_state: { crane: 'on package' },
        end_state: { crane: 'in Mira hand' }
      },
      storyboard_prompt: 'cinematic hallway storyboard',
      video_prompt: 'intimate robot courier reveal',
      salience: 0.92,
      renderer: 'wan-video',
      resolution: '720x1280',
      max_retries: 2,
      quality_threshold: 0.82
    },
    quality: {
      passed: true,
      overall_score: 0.91,
      dimensions: [
        { name: 'continuity', score: 0.94, evidence: 'The red paper crane remains visible through the repaired reveal.' },
        { name: 'composition', score: 0.89, evidence: 'The subject remains centered.' }
      ],
      violations: [],
      repair_strategy: 'none',
      repair_instruction: null,
      evaluator_model: 'qwen-vl',
      attempt: 2
    }
  }
}

function project(overrides: Record<string, unknown> = {}) {
  return {
    id: 'demo-project',
    title: 'The Last Delivery',
    status: 'completed',
    brief: {
      title: 'The Last Delivery',
      premise: 'A courier robot makes one final delivery before dawn.',
      genre: 'science-fiction drama',
      tone: 'cinematic, intimate',
      target_audience: 'global short-form drama viewers',
      duration_seconds: 42,
      aspect_ratio: '9:16',
      language: 'English',
      visual_style: 'grounded cinematic realism',
      budget_usd: 6,
      repair_reserve_percent: 20,
      seed: 20260710,
      required_prop: 'red paper crane'
    },
    plan: {
      title: 'The Last Delivery',
      logline: 'A small courier robot earns one last moment of connection.',
      theme: 'Connection outlasts utility.',
      synopsis: 'Courier-7 completes a final delivery and receives a human goodbye.',
      visual_rules: ['Keep the red paper crane visible.'],
      audio_rules: ['Keep narration sparse.'],
      characters: [],
      beats: [
        { id: 'beat-1', name: 'Arrival', beat_type: 'setup', objective: 'Reach the apartment.', emotional_shift: 'lonely to hopeful', duration_seconds: 8 }
      ],
      shots: []
    },
    ledger: ledger(),
    final_video_url: null,
    error: null,
    created_at: now,
    updated_at: now,
    shots: [shot()],
    events: [
      { id: 1, kind: 'shot.rejected', message: 'S05 continuity failed on first pass.', agent: 'Continuity Supervisor', payload: { shot: 'S05', score: 0.61 }, created_at: now },
      { id: 2, kind: 'shot.accepted', message: 'S05 passed after a local repair.', agent: 'Quality Judge', payload: { shot: 'S05' }, created_at: now }
    ],
    ...overrides
  }
}

function task(status = 'pending', projectId = 'judge-project') {
  return {
    id: 'task-row-1',
    task_id: 'task-judge-1',
    project_id: projectId,
    type: 'judge_test',
    status,
    attempts: 0,
    result: null,
    error: null,
    function_compute_request_id: null,
    durable_status: {
      schema_version: 'task-status/v1',
      project_id: projectId,
      task_id: 'task-judge-1',
      job_id: 'job-judge-1',
      operation: 'judge_test',
      status,
      attempts: 0,
      duplicate: false,
      dispatch_mode: 'function_compute',
      function_compute_request_id: 'fc-request-1',
      function_compute_status_code: 202,
      result: null,
      error: null,
      updated_at: now
    }
  }
}

async function json(route: Route, payload: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(payload)
  })
}

async function installRoutes(page: Page, options: { publicDemo?: boolean; taskProject?: ReturnType<typeof project> } = {}) {
  const demoProject = project()
  const taskProject = options.taskProject ?? project({ id: 'judge-project', title: 'Judge Micro Drama', status: 'queued' })
  await page.route('**/api/config', route => json(route, {
    provider_mode: 'mock',
    live_ready: false,
    oss_ready: true,
    public_demo_project_id: options.publicDemo ? 'demo-project' : null,
    judge_access_code_configured: Boolean(options.publicDemo),
    models: { story: 'qwen-plus', inspection: 'qwen-vl' }
  }))
  await page.route('**/api/projects', route => json(route, options.publicDemo ? [] : [taskProject]))
  await page.route('**/api/public/demo', route => json(route, demoProject))
  await page.route('**/api/projects/judge-project', route => json(route, taskProject))
  await page.route('**/api/projects/demo-project', route => json(route, demoProject))
  await page.route('**/api/tasks/task-judge-1', route => json(route, task('pending')))
}

test('public demo sidebar hides failed setup runs', async ({ page }) => {
  const failedRun = project({ id: 'failed-demo-attempt', title: 'Failed Setup Run', status: 'failed' })
  const demoProject = project()
  await page.route('**/api/config', route => json(route, {
    provider_mode: 'live',
    live_ready: true,
    oss_ready: true,
    public_demo_project_id: 'demo-project',
    judge_access_code_configured: true,
    models: { story: 'qwen-plus', inspection: 'qwen-vl' }
  }))
  await page.route('**/api/projects', route => json(route, [demoProject, failedRun]))
  await page.route('**/api/public/demo', route => json(route, demoProject))

  await page.goto('/')

  await expect(page.getByText('Read-only public demo')).toBeVisible()
  await expect(page.getByText('Failed Setup Run')).toHaveCount(0)
})

test('public demo is read-only and judge test submits with server-side access code', async ({ page }) => {
  let judgeHeader = ''
  await installRoutes(page, { publicDemo: true })
  await page.route('**/api/judge-test', async route => {
    judgeHeader = route.request().headers()['x-directorgraph-judge-code'] ?? ''
    await json(route, { project_id: 'judge-project', job_id: 'job-judge-1', task_id: 'task-judge-1', status: 'pending' })
  })

  await page.goto('/')

  await expect(page.getByText('Read-only public demo')).toBeVisible()
  await expect(page.getByRole('button', { name: /New production/i })).toHaveCount(0)
  await page.getByPlaceholder('Access code').fill('judge-secret')
  await page.getByRole('button', { name: 'Start test' }).click()

  await expect(page.getByRole('heading', { name: 'Judge Micro Drama' })).toBeVisible()
  await expect(page.getByText('Pending')).toBeVisible()
  await expect(page.getByText('task-judge-1')).toBeVisible()
  expect(judgeHeader).toBe('judge-secret')
})

test('active task polling recovers after refresh from browser storage', async ({ page }) => {
  let taskReads = 0
  const queuedProject = project({ id: 'judge-project', title: 'Judge Micro Drama', status: 'queued' })
  await installRoutes(page, { taskProject: queuedProject })
  await page.route('**/api/tasks/task-judge-1', route => {
    taskReads += 1
    return json(route, task('pending'))
  })
  await page.addInitScript(() => {
    window.localStorage.setItem('directorgraph.activeTask', JSON.stringify({ project_id: 'judge-project', task_id: 'task-judge-1' }))
  })

  await page.goto('/')
  await expect(page.getByText('task-judge-1')).toBeVisible()
  await page.reload()
  await expect(page.getByText('task-judge-1')).toBeVisible()
  await expect.poll(() => taskReads).toBeGreaterThanOrEqual(2)
})

test('semantic patch request starts a deterministic task from completed project', async ({ page }) => {
  let patchBody: Record<string, unknown> | null = null
  await installRoutes(page, { taskProject: project() })
  await page.route('**/api/projects/demo-project/patch', async route => {
    patchBody = route.request().postDataJSON() as Record<string, unknown>
    await json(route, { project_id: 'demo-project', job_id: 'job-patch-1', task_id: 'task-judge-1', status: 'pending' })
  })
  await page.route('**/api/tasks/task-judge-1', route => json(route, task('pending', 'demo-project')))

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'The Last Delivery' })).toBeVisible()
  await page.getByRole('button', { name: 'Analyze impact & patch' }).click()

  await expect(page.getByText('Pending')).toBeVisible()
  expect(String(patchBody?.instruction)).toContain('dawn warmer')
})

test('film view opens with judge proof cockpit before media player', async ({ page }) => {
  await installRoutes(page, { publicDemo: true })

  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Judge proof cockpit' })).toBeVisible()
  await expect(page.getByText('Contract-first production')).toBeVisible()
  await expect(page.getByText('Qwen-VL inspection loop')).toBeVisible()
  await expect(page.getByText('Media player is secondary')).toBeVisible()

  const cockpitTop = await page.locator('.film-cockpit').evaluate(el => el.getBoundingClientRect().top)
  const playerTop = await page.locator('.player-panel').evaluate(el => el.getBoundingClientRect().top)
  expect(cockpitTop).toBeLessThan(playerTop)
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(1440)
})

test('story shots and evidence tabs expose rich judge surfaces', async ({ page }) => {
  await installRoutes(page, { publicDemo: true })

  await page.goto('/')

  await page.getByRole('tab', { name: 'Story' }).click()
  await expect(page.getByRole('heading', { name: 'Story map' })).toBeVisible()
  await expect(page.getByText('Locked continuity rules')).toBeVisible()

  await page.getByRole('tab', { name: 'Shots' }).click()
  await expect(page.getByRole('heading', { name: 'Shot contract board' })).toBeVisible()
  await expect(page.getByText('Renderer mix')).toBeVisible()

  await page.getByRole('tab', { name: 'Evidence' }).click()
  await expect(page.getByRole('heading', { name: 'Evidence ledger' })).toBeVisible()
  await expect(page.getByText('Budget and repair accounting')).toBeVisible()
})

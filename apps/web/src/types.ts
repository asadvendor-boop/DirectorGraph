export type ProjectStatus = 'draft' | 'queued' | 'planning' | 'storyboarding' | 'producing' | 'inspecting' | 'editing' | 'completed' | 'failed'
export type ShotStatus = 'planned' | 'storyboarding' | 'storyboarded' | 'rendering' | 'inspecting' | 'repairing' | 'accepted' | 'failed'
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled'

export interface ProjectBrief {
  title: string
  premise: string
  genre: string
  tone: string
  target_audience: string
  duration_seconds: number
  aspect_ratio: '9:16' | '16:9' | '1:1'
  language: string
  visual_style: string
  budget_usd: number
  repair_reserve_percent: number
  seed: number
  required_prop?: string | null
  max_shots?: number | null
  production_profile?: 'standard' | 'judge_test'
}

export interface CameraSpec { framing: string; movement: string; angle: string; lens: string }
export interface ContinuitySpec {
  location: string
  time_of_day: string
  wardrobe: Record<string, string>
  required_props: string[]
  start_state: Record<string, string>
  end_state: Record<string, string>
}
export interface ShotContract {
  id: string
  sequence: number
  beat_id: string
  title: string
  duration_seconds: number
  aspect_ratio: string
  narrative_objective: string
  characters: string[]
  action: string
  dialogue?: string | null
  narration?: string | null
  emotion: string
  location: string
  camera: CameraSpec
  continuity: ContinuitySpec
  storyboard_prompt: string
  video_prompt: string
  salience: number
  renderer: string
  resolution: string
  max_retries: number
  quality_threshold: number
}
export interface QualityDimension { name: string; score: number; evidence: string }
export interface QualityReport {
  passed: boolean
  overall_score: number
  dimensions: QualityDimension[]
  violations: string[]
  repair_strategy: string
  repair_instruction?: string | null
  evaluator_model: string
  attempt: number
}
export interface Shot {
  id: string
  shot_code: string
  sequence: number
  status: ShotStatus
  contract: ShotContract
  storyboard_url?: string | null
  audio_url?: string | null
  video_url?: string | null
  quality?: QualityReport | null
  attempts: number
  accepted: boolean
}
export interface EventItem {
  id: number
  kind: string
  message: string
  agent: string
  payload: Record<string, unknown>
  created_at: string
}
export interface Ledger {
  text_input_tokens: number
  text_output_tokens: number
  vision_input_tokens: number
  video_seconds_generated: number
  video_seconds_accepted: number
  rejected_generation_seconds: number
  image_count: number
  full_regenerations: number
  local_repairs: number
  estimated_cost_usd: number
  budget_usd: number
  repair_reserve_usd: number
  budget_remaining_usd: number
  acceptance_ratio: number
}
export interface Character { id: string; name: string; role: string; appearance: string; wardrobe: string; voice_direction: string; motivation: string; reference_prompt: string; reference_url?: string | null }
export interface Beat { id: string; name: string; beat_type: string; objective: string; emotional_shift: string; duration_seconds: number }
export interface StoryPlan {
  title: string
  logline: string
  theme: string
  synopsis: string
  visual_rules: string[]
  audio_rules: string[]
  characters: Character[]
  beats: Beat[]
  shots: ShotContract[]
}
export interface Project {
  id: string
  title: string
  status: ProjectStatus
  brief: ProjectBrief
  plan?: StoryPlan | null
  ledger: Ledger
  final_video_url?: string | null
  error?: string | null
  created_at: string
  updated_at: string
  shots: Shot[]
  events: EventItem[]
}
export interface RunResponse {
  project_id: string
  job_id: string
  task_id: string
  status: JobStatus
}
export interface TaskStatusSnapshot {
  schema_version: string
  project_id: string
  task_id: string
  job_id: string
  operation: string
  status: JobStatus
  attempts: number
  duplicate: boolean
  dispatch_mode?: string | null
  function_compute_request_id?: string | null
  function_compute_status_code?: number | null
  result?: Record<string, unknown> | null
  error?: string | null
  updated_at: string
}
export interface TaskRead {
  id: string
  task_id: string
  project_id: string
  type: string
  status: JobStatus
  attempts: number
  result?: Record<string, unknown> | null
  error?: string | null
  function_compute_request_id?: string | null
  durable_status?: TaskStatusSnapshot | null
}
export interface PublicConfig {
  provider_mode: string
  live_ready: boolean
  oss_ready: boolean
  public_demo_project_id?: string | null
  judge_access_code_configured: boolean
  models: Record<string, string>
}

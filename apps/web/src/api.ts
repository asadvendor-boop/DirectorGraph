import type { Project, ProjectBrief, PublicConfig, RunResponse, TaskRead } from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) }
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(payload.detail ?? `Request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export const api = {
  config: () => request<PublicConfig>('/api/config'),
  publicDemo: () => request<Project>('/api/public/demo'),
  projects: () => request<Project[]>('/api/projects'),
  project: (id: string) => request<Project>(`/api/projects/${id}`),
  create: (brief: ProjectBrief) => request<Project>('/api/projects', { method: 'POST', body: JSON.stringify(brief) }),
  run: (id: string) => request<RunResponse>(`/api/projects/${id}/run`, { method: 'POST' }),
  patch: (id: string, instruction: string, affected_shot_ids: string[] = []) => request<RunResponse>(`/api/projects/${id}/patch`, {
    method: 'POST',
    body: JSON.stringify({ instruction, affected_shot_ids })
  }),
  judgeTest: (judgeCode: string) => request<RunResponse>('/api/judge-test', {
    method: 'POST',
    headers: judgeCode ? { 'X-DirectorGraph-Judge-Code': judgeCode } : undefined,
    body: JSON.stringify({})
  }),
  task: (taskId: string) => request<TaskRead>(`/api/tasks/${taskId}`),
  stopTask: (taskId: string, judgeCode = '') => request<TaskRead>(`/api/tasks/${taskId}/stop`, {
    method: 'POST',
    headers: judgeCode ? { 'X-DirectorGraph-Judge-Code': judgeCode } : undefined
  }),
  manifestUrl: (id: string) => `${API_BASE}/api/projects/${id}/manifest`,
  publicDemoManifestUrl: () => `${API_BASE}/api/public/demo/storage-manifest`
}

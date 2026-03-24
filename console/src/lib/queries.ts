import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './api';
import type { JobsResponse, FleetStatus, TeamsResponse, FinanceSummary, FinanceReport, ScheduledJob, CompanySummary, Job, SchedulerTask } from './types';

export function useFleetStatus() {
  return useQuery({
    queryKey: ['fleet-status'],
    queryFn: () => apiFetch<FleetStatus>('/fleet/status'),
    staleTime: 30_000,
  });
}

export function useCompanies() {
  return useQuery({
    queryKey: ['companies'],
    queryFn: () => apiFetch<CompanySummary[]>('/companies'),
    staleTime: 30_000,
  });
}

export function useJobs(params?: { status?: string; company_id?: string; limit?: number; offset?: number }) {
  const searchParams = new URLSearchParams();
  if (params?.status) searchParams.set('status', params.status);
  if (params?.company_id) searchParams.set('company_id', params.company_id);
  if (params?.limit) searchParams.set('limit', String(params.limit));
  if (params?.offset) searchParams.set('offset', String(params.offset));
  const qs = searchParams.toString();
  return useQuery({
    queryKey: ['jobs', params],
    queryFn: () => apiFetch<JobsResponse>(`/jobs${qs ? `?${qs}` : ''}`),
    staleTime: 30_000,
  });
}

export function useJob(jobId: string) {
  return useQuery({
    queryKey: ['job', jobId],
    queryFn: () => apiFetch<Job>(`/jobs/${jobId}`),
    enabled: !!jobId,
  });
}

export function useTeams() {
  return useQuery({
    queryKey: ['teams'],
    queryFn: () => apiFetch<TeamsResponse>('/teams'),
    staleTime: 30_000,
  });
}

export function useSchedules() {
  return useQuery({
    queryKey: ['schedules'],
    queryFn: () => apiFetch<ScheduledJob[]>('/schedules'),
    staleTime: 30_000,
  });
}

export function useSchedulerStatus() {
  return useQuery({
    queryKey: ['scheduler-status'],
    queryFn: () => apiFetch<{ tasks: SchedulerTask[] }>('/scheduler/status'),
    staleTime: 30_000,
  });
}

export function useFinanceSummary() {
  return useQuery({
    queryKey: ['finance-summary'],
    queryFn: () => apiFetch<FinanceSummary>('/finance/summary'),
    staleTime: 30_000,
  });
}

export function useFinanceReport(hours: number = 24) {
  return useQuery({
    queryKey: ['finance-report', hours],
    queryFn: () => apiFetch<FinanceReport>(`/finance/report?hours=${hours}`),
    staleTime: 30_000,
  });
}

export function useBindings() {
  return useQuery({
    queryKey: ['bindings'],
    queryFn: () => apiFetch<Record<string, unknown>[]>('/bindings'),
    staleTime: 30_000,
  });
}

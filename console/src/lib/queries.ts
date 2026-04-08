import { useQuery } from '@tanstack/react-query';
import { apiFetch } from './api';
import type {
  JobsResponse, FleetStatus, TeamsResponse,
  FinanceSummary, FinanceReport,
  CompaniesResponse, SchedulesResponse, SchedulerStatusResponse,
  BindingsResponse, Job, CompanyDetail, MetaResponse, CompanyJobsResponse, EventsResponse,
} from './types';

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
    queryFn: () => apiFetch<CompaniesResponse>('/companies'),
    staleTime: 30_000,
  });
}

export function useCompany(companyId: string) {
  return useQuery({
    queryKey: ['company', companyId],
    queryFn: () => apiFetch<CompanyDetail>(`/companies/${companyId}`),
    enabled: !!companyId,
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
    staleTime: 30_000,
  });
}

export function useCompanyJobs(companyId: string, params?: { status?: string; limit?: number }) {
  const searchParams = new URLSearchParams();
  if (params?.status) searchParams.set('status', params.status);
  if (params?.limit) searchParams.set('limit', String(params.limit));
  const qs = searchParams.toString();
  return useQuery({
    queryKey: ['company-jobs', companyId, params],
    queryFn: () => apiFetch<CompanyJobsResponse>(`/companies/${companyId}/jobs${qs ? `?${qs}` : ''}`),
    enabled: !!companyId,
    staleTime: 30_000,
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
    queryFn: () => apiFetch<SchedulesResponse>('/schedules'),
    staleTime: 30_000,
  });
}

export function useSchedulerStatus() {
  return useQuery({
    queryKey: ['scheduler-status'],
    queryFn: () => apiFetch<SchedulerStatusResponse>('/scheduler/status'),
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
    queryFn: () => apiFetch<BindingsResponse>('/bindings'),
    staleTime: 30_000,
  });
}

export function useMeta() {
  return useQuery({
    queryKey: ['meta'],
    queryFn: () => apiFetch<MetaResponse>('/meta'),
    staleTime: 30_000,
  });
}

export function useEvents(params?: {
  event_type?: string;
  company_id?: string;
  hours?: number;
  start?: string;
  end?: string;
  limit?: number;
  offset?: number;
}) {
  const searchParams = new URLSearchParams();
  if (params?.event_type) searchParams.set('event_type', params.event_type);
  if (params?.company_id) searchParams.set('company_id', params.company_id);
  if (params?.hours) searchParams.set('hours', String(params.hours));
  if (params?.start) searchParams.set('start', params.start);
  if (params?.end) searchParams.set('end', params.end);
  if (params?.limit) searchParams.set('limit', String(params.limit));
  if (params?.offset) searchParams.set('offset', String(params.offset));
  const qs = searchParams.toString();

  return useQuery({
    queryKey: ['events', params],
    queryFn: () => apiFetch<EventsResponse>(`/events${qs ? `?${qs}` : ''}`),
    staleTime: 10_000,
  });
}

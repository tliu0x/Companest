import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiPost, apiDelete, apiPatch, apiPut } from './api';

export function useCancelJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => apiPost(`/jobs/${jobId}/cancel`),
    onSuccess: (_, jobId) => {
      qc.invalidateQueries({ queryKey: ['job', jobId] });
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}

export function useTriggerSchedulerTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (taskName: string) => apiPost(`/scheduler/${taskName}/trigger`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['scheduler-status'] });
      qc.invalidateQueries({ queryKey: ['company'] });
    },
  });
}

export function useCancelSchedule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (scheduleId: string) => apiDelete(`/schedules/${scheduleId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['schedules'] });
    },
  });
}

export function useResetCircuitBreaker() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost('/finance/circuit-breaker/reset'),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['finance-summary'] });
      qc.invalidateQueries({ queryKey: ['finance-report'] });
    },
  });
}

export function useResolveApproval() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ approvalId, choice }: { approvalId: string; choice: 'approve' | 'downgrade' | 'reject' }) =>
      apiPost(`/finance/approve/${approvalId}`, { choice }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['finance-summary'] });
      qc.invalidateQueries({ queryKey: ['finance-report'] });
      qc.invalidateQueries({ queryKey: ['jobs'] });
    },
  });
}

export function useCreateCompany() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: Record<string, unknown>) => apiPost('/companies', data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['companies'] });
      qc.invalidateQueries({ queryKey: ['fleet-status'] });
    },
  });
}

export function useUpdateCompany() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      apiPatch(`/companies/${id}`, data),
    onSuccess: (_, { id }) => {
      qc.invalidateQueries({ queryKey: ['companies'] });
      qc.invalidateQueries({ queryKey: ['company', id] });
      qc.invalidateQueries({ queryKey: ['fleet-status'] });
    },
  });
}

export function useDeleteCompany() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => apiDelete(`/companies/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['companies'] });
      qc.invalidateQueries({ queryKey: ['fleet-status'] });
      qc.invalidateQueries({ queryKey: ['company'] });
    },
  });
}

export function useToggleCompany() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      apiPatch(`/companies/${id}`, { enabled }),
    onSuccess: (_, { id }) => {
      qc.invalidateQueries({ queryKey: ['companies'] });
      qc.invalidateQueries({ queryKey: ['company', id] });
      qc.invalidateQueries({ queryKey: ['fleet-status'] });
    },
  });
}

export function useAddCompanyBinding() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ companyId, data }: { companyId: string; data: Record<string, unknown> }) =>
      apiPost(`/companies/${companyId}/bind`, data),
    onSuccess: (_, { companyId }) => {
      qc.invalidateQueries({ queryKey: ['company', companyId] });
      qc.invalidateQueries({ queryKey: ['companies'] });
    },
  });
}

export function useSetBindings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (bindings: unknown[]) => apiPut('/bindings', bindings),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bindings'] });
    },
  });
}

export function useRunTeamTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ teamId, data }: { teamId: string; data: Record<string, unknown> }) =>
      apiPost(`/teams/${teamId}/run`, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] });
      qc.invalidateQueries({ queryKey: ['teams'] });
    },
  });
}

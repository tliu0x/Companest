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

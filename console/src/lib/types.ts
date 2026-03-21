export interface Job {
  id: string;
  task: string;
  status: string;
  context: Record<string, unknown> | null;
  subtasks: unknown[] | null;
  result: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  submitted_by: string;
  company_id: string | null;
}

export interface JobsResponse {
  jobs: Job[];
  total: number;
  limit: number;
  offset: number;
  stats: Record<string, number>;
}

export interface CompanySummary {
  id: string;
  name: string;
  domain: string;
  enabled: boolean;
  bindings_count: number;
  ceo_enabled: boolean;
}

export interface CompanyDetail {
  config: Record<string, unknown>;
  teams: string[];
  schedule_status: Record<string, unknown>[];
  recent_jobs: Record<string, unknown>[];
}

export interface FleetStatus {
  total_companies: number;
  active_teams: number;
  total_jobs: number;
  jobs_by_status: Record<string, number>;
  companies: Record<string, {
    name: string;
    enabled: boolean;
    teams: number;
    active_jobs: number;
    total_jobs: number;
  }>;
}

export interface TeamConfig {
  id: string;
  role: string;
  lead_pi: string;
  mode: string;
  enabled: boolean;
  always_on: boolean;
  pis: unknown[];
}

export interface TeamsResponse {
  configs: Record<string, TeamConfig>;
  active: string[];
}

export interface ScheduledJob {
  id: string;
  task: string;
  description: string;
  trigger_type: string;
  trigger_args: Record<string, unknown>;
  team_id: string | null;
  mode: string | null;
  fire_count: number;
  last_fired: string | null;
  created_at: string;
  active: boolean;
}

export interface SchedulerTask {
  name: string;
  interval: string;
  last_run: string | null;
  next_run: string | null;
  enabled: boolean;
}

export interface FinanceSummary {
  total_spent: number;
  budget_remaining: number | null;
  daily_limit: number | null;
  circuit_breaker: {
    tripped: boolean;
    trip_count: number;
  };
  [key: string]: unknown;
}

export interface FinanceReport {
  hours: number;
  entries: Record<string, unknown>[];
  [key: string]: unknown;
}

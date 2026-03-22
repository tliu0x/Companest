// ---- Jobs ----

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

// ---- Companies ----

export interface CompanySummary {
  id: string;
  name: string;
  domain: string;
  enabled: boolean;
  bindings_count: number;
  ceo_enabled: boolean;
}

export interface CompaniesResponse {
  companies: CompanySummary[];
  total: number;
}

// ---- Fleet Status ----
// GET /api/fleet/status returns: jobs (stats object), teams, companies, timestamp

export interface FleetStatus {
  jobs: {
    total: number;
    pending: number;
    queued: number;
    running: number;
    waiting_approval: number;
    completed: number;
    failed: number;
    cancelled: number;
    queue_size: number;
  };
  timestamp: string;
  teams?: {
    registered: string[];
    active: string[];
    configs: Record<string, {
      role: string;
      mode: string;
      always_on: boolean;
      pi_count: number;
      lead_pi: string;
    }>;
  };
  companies?: Record<string, {
    name: string;
    enabled: boolean;
    active_teams: number;
    total_jobs: number;
  }>;
}

// ---- Teams ----
// GET /api/teams returns fleet_status directly (no envelope)

export interface TeamConfig {
  role: string;
  mode: string;
  always_on: boolean;
  pi_count: number;
  lead_pi: string;
}

export interface TeamsResponse {
  registered: string[];
  active: string[];
  configs: Record<string, TeamConfig>;
}

// ---- Schedules ----
// GET /api/schedules returns { schedules: [...], total, status }

export interface ScheduledJob {
  id: string;
  user_id: string;
  chat_id: string;
  channel: string;
  task: string;
  description: string;
  trigger_type: string;
  trigger_args: Record<string, unknown>;
  team_id: string | null;
  mode: string;
  fire_count: number;
  last_fired: string | null;
  created_at: string;
  active: boolean;
}

export interface SchedulesResponse {
  schedules: ScheduledJob[];
  total: number;
  status: {
    started: boolean;
    db_path: string;
    active_jobs: number;
    next_run: string | null;
  };
}

// GET /api/scheduler/status returns { started, tasks: { name: {...} } }

export interface SchedulerTaskInfo {
  enabled: boolean;
  interval_seconds: number;
  last_run: string | null;
  run_count: number;
  error_count: number;
  last_error: string | null;
  running: boolean;
}

export interface SchedulerStatusResponse {
  started: boolean;
  tasks: Record<string, SchedulerTaskInfo>;
}

// ---- Finance ----
// GET /api/finance/summary — values are in dollars (float), NOT cents

export interface CircuitBreakerInfo {
  tripped: boolean;
  window_spend: number;
  window_minutes: number;
  threshold_pct: number;
  cooldown_minutes: number;
  cooldown_remaining_seconds: number;
  events_in_window: number;
}

export interface FinanceSummary {
  total: number;
  by_team: Record<string, number>;
  entries: number;
  days: number;
  today: number;
  window_spend: number;
  budget: {
    daily_limit: number;
    mode: string;
    rolling_window_hours: number;
    team_budgets: Record<string, unknown>;
    overflow_pool: number;
  };
  source: string;
  mode: string;
  circuit_breaker: CircuitBreakerInfo | null;
}

// GET /api/finance/report — values are in dollars (float)

export interface FinanceReport {
  window_hours: number;
  window_spend: number;
  daily_limit: number;
  utilization_pct: number;
  by_team: Record<string, number>;
  team_utilization: Record<string, {
    spent: number;
    budget: number;
    utilization_pct: number;
  }>;
  mode: string;
  circuit_breaker: CircuitBreakerInfo | null;
  overflow_pool: number;
  overflow_used: number;
}

// ---- Bindings ----
// GET /api/bindings returns { bindings: [...] }

export interface GlobalBinding {
  channel?: string;
  chat_id?: string;
  user_id?: string;
  team_id: string;
  mode: string;
  priority: number;
}

export interface BindingsResponse {
  bindings: GlobalBinding[];
}

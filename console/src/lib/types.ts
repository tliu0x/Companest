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

export interface CompanyJobsResponse {
  jobs: Job[];
  total: number;
}

export interface ConsoleEvent {
  sequence_id: number;
  type: string;
  timestamp: string;
  company_id: string | null;
  payload: Record<string, unknown>;
}

export interface EventsResponse {
  events: ConsoleEvent[];
  total: number;
  limit: number;
  offset: number;
  latest_sequence_id: number;
  note?: string;
}

// ---- Companies ----

export interface CompanySummary {
  id: string;
  name: string;
  domain: string;
  enabled: boolean;
  bindings_count: number;
  ceo_enabled: boolean;
  active_team_count: number;
  recent_job_count: number;
  last_activity_timestamp: string | null;
}

export interface CompaniesResponse {
  companies: CompanySummary[];
  total: number;
  note?: string;
}

export interface CompanyBinding {
  channel?: string | null;
  chat_id?: string | null;
  user_id?: string | null;
}

export interface CompanyScheduleConfig {
  name: string;
  team_id: string;
  prompt: string;
  interval_seconds: number;
  mode: string;
  enabled: boolean;
}

export interface CompanyPreferences {
  default_mode: string;
  preferred_teams: string[];
  budget_hourly_usd: number;
  budget_monthly_usd: number;
}

export interface CompanyCEOConfig {
  model: string;
  max_turns: number;
  cycle_interval: number;
  cycle_prompt?: string | null;
  goals: string[];
  kpis: Record<string, string>;
  enabled: boolean;
}

export interface CompanyFinanceSummary {
  hourly_budget_usd: number;
  monthly_budget_usd: number;
  last_hour_spend: number;
  last_24h_spend: number;
}

export interface CompanyDetail {
  id: string;
  name: string;
  domain: string;
  enabled: boolean;
  bindings: CompanyBinding[];
  preferences: CompanyPreferences;
  ceo: CompanyCEOConfig;
  schedules: CompanyScheduleConfig[];
  env: Record<string, string>;
  shared_teams: string[] | null;
  routing_bindings: Array<Record<string, string>>;
  memory_seed: Record<string, unknown>;
  mcp_servers: Array<Record<string, unknown>>;
  teams: string[];
  schedule_status: Record<string, SchedulerTaskInfo>;
  recent_jobs: Job[];
  summary: CompanySummary;
  finance?: CompanyFinanceSummary;
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
  note?: string;
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
  note?: string;
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
  note?: string;
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
  pending_approvals: PendingApproval[];
  pending_approvals_count: number;
  note?: string;
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
  pending_approvals_count?: number;
  note?: string;
}

export interface PendingApproval {
  approval_id: string;
  task: string;
  reason: string;
  priority: string;
  estimated_cost_usd: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  target_team: string;
  target_model: string;
  suggested_downgrade?: string | null;
  today_spent: number;
  daily_limit: number;
  created_at: string;
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

// ---- Meta ----

export interface ApiCapabilities {
  admin: boolean;
  companies: boolean;
  finance: boolean;
  scheduler: boolean;
  user_scheduler: boolean;
  websocket: boolean;
  events_history: boolean;
  public_knowledge: boolean;
}

export interface MetaResponse {
  service: string;
  version: string;
  timestamp: string;
  capabilities: ApiCapabilities;
}

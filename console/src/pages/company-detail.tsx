/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { ArrowLeft } from 'lucide-react';
import { getErrorMessage } from '@/lib/api';
import { useRealtime } from '@/lib/realtime';
import { useCompany, useCompanyJobs } from '@/lib/queries';
import {
  useAddCompanyBinding,
  useDeleteCompany,
  useToggleCompany,
  useUpdateCompany,
} from '@/lib/mutations';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { StatusBadge } from '@/components/shared/status-badge';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type { CompanyDetail } from '@/lib/types';

type CompanyTab = 'summary' | 'config' | 'teams' | 'jobs' | 'schedules' | 'finance' | 'bindings' | 'raw';

const COMPANY_TABS: Array<{ key: CompanyTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'config', label: 'Config' },
  { key: 'teams', label: 'Teams' },
  { key: 'jobs', label: 'Jobs' },
  { key: 'schedules', label: 'Schedules' },
  { key: 'finance', label: 'Finance' },
  { key: 'bindings', label: 'Bindings' },
  { key: 'raw', label: 'Raw JSON' },
];

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '-';
  return new Date(value).toLocaleString();
}

function formatDollars(value: number | null | undefined): string {
  if (value == null) return '-';
  return `$${value.toFixed(2)}`;
}

function buildUpdatePayload(company: CompanyDetail, form: {
  name: string;
  domain: string;
  enabled: boolean;
  ceoEnabled: boolean;
  defaultMode: string;
  budgetHourlyUsd: string;
  budgetMonthlyUsd: string;
}) {
  const payload: Record<string, unknown> = {};
  const trimmedName = form.name.trim();
  const trimmedDomain = form.domain.trim();
  const trimmedDefaultMode = form.defaultMode.trim();
  const hourlyBudget = Number(form.budgetHourlyUsd);
  const monthlyBudget = Number(form.budgetMonthlyUsd);

  if (trimmedName !== company.name) payload.name = trimmedName;
  if (trimmedDomain !== company.domain) payload.domain = trimmedDomain;
  if (form.enabled !== company.enabled) payload.enabled = form.enabled;

  const ceoPatch: Record<string, unknown> = {};
  if (form.ceoEnabled !== company.ceo.enabled) ceoPatch.enabled = form.ceoEnabled;
  if (Object.keys(ceoPatch).length > 0) payload.ceo = ceoPatch;

  const preferencesPatch: Record<string, unknown> = {};
  if (trimmedDefaultMode !== company.preferences.default_mode) {
    preferencesPatch.default_mode = trimmedDefaultMode;
  }
  if (Number.isFinite(hourlyBudget) && hourlyBudget !== company.preferences.budget_hourly_usd) {
    preferencesPatch.budget_hourly_usd = hourlyBudget;
  }
  if (Number.isFinite(monthlyBudget) && monthlyBudget !== company.preferences.budget_monthly_usd) {
    preferencesPatch.budget_monthly_usd = monthlyBudget;
  }
  if (Object.keys(preferencesPatch).length > 0) payload.preferences = preferencesPatch;

  return payload;
}

function DeleteCompanyDialog({
  companyId,
  companyName,
  onClose,
  onDelete,
  isPending,
  error,
}: {
  companyId: string;
  companyName: string;
  onClose: () => void;
  onDelete: () => void;
  isPending: boolean;
  error?: string;
}) {
  const [confirmText, setConfirmText] = useState('');

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-background border rounded-lg p-6 space-y-4 max-w-sm w-full mx-4">
        <h3 className="text-lg font-semibold">Delete Company</h3>
        <p className="text-sm text-muted-foreground">
          Type <span className="font-mono font-bold">{companyId}</span> to delete <span className="font-semibold">{companyName}</span>.
        </p>
        {error && <ErrorAlert message={error} />}
        <Input
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
          placeholder={companyId}
        />
        <div className="flex gap-2 justify-end">
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button
            variant="destructive"
            disabled={confirmText !== companyId || isPending}
            onClick={onDelete}
          >
            {isPending ? 'Deleting...' : 'Delete'}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function CompanyDetailPage() {
  const { companyId } = useParams({ from: '/layout/console/companies/$companyId' as const });
  const navigate = useNavigate();
  const { data: company, isLoading, error } = useCompany(companyId);
  const jobsQuery = useCompanyJobs(companyId, { limit: 20 });
  const updateCompany = useUpdateCompany();
  const toggleCompany = useToggleCompany();
  const deleteCompany = useDeleteCompany();
  const addBinding = useAddCompanyBinding();
  const { getCompanyActivityCount, markCompanySeen } = useRealtime();

  const [activeTab, setActiveTab] = useState<CompanyTab>('summary');
  const [showDelete, setShowDelete] = useState(false);
  const [summaryMessage, setSummaryMessage] = useState<string | null>(null);
  const [bindingMessage, setBindingMessage] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [formState, setFormState] = useState({
    name: '',
    domain: '',
    enabled: true,
    ceoEnabled: true,
    defaultMode: 'cascade',
    budgetHourlyUsd: '0',
    budgetMonthlyUsd: '0',
  });
  const [bindingForm, setBindingForm] = useState({
    channel: '',
    chat_id: '',
    user_id: '',
  });

  useEffect(() => (
    () => {
      markCompanySeen(companyId);
    }
  ), [companyId, markCompanySeen]);

  useEffect(() => {
    if (!company) return;
    setFormState({
      name: company.name,
      domain: company.domain,
      enabled: company.enabled,
      ceoEnabled: company.ceo.enabled,
      defaultMode: company.preferences.default_mode,
      budgetHourlyUsd: String(company.preferences.budget_hourly_usd),
      budgetMonthlyUsd: String(company.preferences.budget_monthly_usd),
    });
    setBindingForm({ channel: '', chat_id: '', user_id: '' });
    setFormError(null);
    setBindingError(null);
  }, [company]);

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={getErrorMessage(error)} />;
  if (!company) return <ErrorAlert message="Company not found" />;

  const updatePayload = buildUpdatePayload(company, formState);
  const updatePreview = Object.keys(updatePayload).length > 0 ? updatePayload : null;
  const jobs = jobsQuery.data?.jobs ?? company.recent_jobs ?? [];
  const scheduleEntries = Object.entries(company.schedule_status ?? {});
  const companyEnabled = company.enabled;
  const activityCount = getCompanyActivityCount(companyId);

  function handleSubmitUpdate(e: React.FormEvent) {
    e.preventDefault();
    setSummaryMessage(null);

    if (!formState.name.trim()) {
      setFormError('Company name is required.');
      return;
    }

    if (!formState.defaultMode.trim()) {
      setFormError('Default mode is required.');
      return;
    }

    if (!Number.isFinite(Number(formState.budgetHourlyUsd)) || Number(formState.budgetHourlyUsd) < 0) {
      setFormError('Hourly budget must be a valid non-negative number.');
      return;
    }

    if (!Number.isFinite(Number(formState.budgetMonthlyUsd)) || Number(formState.budgetMonthlyUsd) < 0) {
      setFormError('Monthly budget must be a valid non-negative number.');
      return;
    }

    if (!updatePreview) {
      setFormError('No changes to save.');
      return;
    }

    setFormError(null);
    updateCompany.mutate(
      { id: companyId, data: updatePreview },
      {
        onSuccess: () => setSummaryMessage('Company updated.'),
        onError: (mutationError) => setSummaryMessage(`Update failed: ${getErrorMessage(mutationError)}`),
      },
    );
  }

  function handleToggleCompany() {
    setSummaryMessage(null);
    toggleCompany.mutate(
      { id: companyId, enabled: !companyEnabled },
      {
        onSuccess: () => setSummaryMessage(companyEnabled ? 'Company disabled.' : 'Company enabled.'),
        onError: (mutationError) => setSummaryMessage(`Status change failed: ${getErrorMessage(mutationError)}`),
      },
    );
  }

  function handleDeleteCompany() {
    deleteCompany.mutate(companyId, {
      onSuccess: () => navigate({ to: '/console/companies' }),
    });
  }

  function handleAddBinding(e: React.FormEvent) {
    e.preventDefault();
    setBindingMessage(null);

    const payload = {
      channel: bindingForm.channel.trim() || undefined,
      chat_id: bindingForm.chat_id.trim() || undefined,
      user_id: bindingForm.user_id.trim() || undefined,
    };

    if (!payload.channel && !payload.chat_id && !payload.user_id) {
      setBindingError('At least one binding field is required.');
      return;
    }

    setBindingError(null);
    addBinding.mutate(
      { companyId, data: payload },
      {
        onSuccess: () => {
          setBindingForm({ channel: '', chat_id: '', user_id: '' });
          setBindingMessage('Binding added.');
        },
        onError: (mutationError) => setBindingMessage(`Binding failed: ${getErrorMessage(mutationError)}`),
      },
    );
  }

  return (
    <div className="p-6 space-y-6">
      <Link to="/console/companies">
        <Button variant="ghost" size="sm">
          <ArrowLeft className="h-4 w-4 mr-1" />
          Back to Companies
        </Button>
      </Link>

      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-2xl font-semibold">{company.name}</h2>
            <Badge
              variant="outline"
              className={company.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
            >
              {company.enabled ? 'enabled' : 'disabled'}
            </Badge>
            {activityCount > 0 && (
              <Badge variant="outline" className="bg-amber-100 text-amber-900">
                {activityCount} new events
              </Badge>
            )}
            <span className="text-sm text-muted-foreground font-mono">{company.id}</span>
          </div>
          <p className="text-sm text-muted-foreground">
            Last activity: {formatTimestamp(company.summary.last_activity_timestamp)}
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <JsonDrawer title="Company JSON" data={company} />
          <Button
            variant="outline"
            disabled={toggleCompany.isPending}
            onClick={handleToggleCompany}
          >
            {toggleCompany.isPending ? 'Saving...' : company.enabled ? 'Disable' : 'Enable'}
          </Button>
          <Button variant="destructive" onClick={() => setShowDelete(true)}>
            Delete
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        {COMPANY_TABS.map((tab) => (
          <Button
            key={tab.key}
            variant={activeTab === tab.key ? 'default' : 'outline'}
            size="sm"
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </Button>
        ))}
      </div>

      {activeTab === 'summary' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Active Teams</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold">{company.summary.active_team_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Recent Jobs</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-2xl font-bold">{company.summary.recent_job_count}</p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm text-muted-foreground">Domain</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm break-words">{company.domain || '-'}</p>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Edit Company</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {formError && <ErrorAlert message={formError} />}
              {summaryMessage && (
                <p className={`text-sm ${summaryMessage.includes('failed') ? 'text-destructive' : 'text-green-600'}`}>
                  {summaryMessage}
                </p>
              )}

              <form className="space-y-4" onSubmit={handleSubmitUpdate}>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="company-name">Name</Label>
                    <Input
                      id="company-name"
                      value={formState.name}
                      onChange={(e) => setFormState((current) => ({ ...current, name: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="company-domain">Domain</Label>
                    <Input
                      id="company-domain"
                      value={formState.domain}
                      onChange={(e) => setFormState((current) => ({ ...current, domain: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="company-mode">Default Mode</Label>
                    <Input
                      id="company-mode"
                      value={formState.defaultMode}
                      onChange={(e) => setFormState((current) => ({ ...current, defaultMode: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="company-hourly-budget">Hourly Budget (USD)</Label>
                    <Input
                      id="company-hourly-budget"
                      type="number"
                      min="0"
                      step="0.01"
                      value={formState.budgetHourlyUsd}
                      onChange={(e) => setFormState((current) => ({ ...current, budgetHourlyUsd: e.target.value }))}
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="company-monthly-budget">Monthly Budget (USD)</Label>
                    <Input
                      id="company-monthly-budget"
                      type="number"
                      min="0"
                      step="0.01"
                      value={formState.budgetMonthlyUsd}
                      onChange={(e) => setFormState((current) => ({ ...current, budgetMonthlyUsd: e.target.value }))}
                    />
                  </div>
                </div>

                <div className="flex flex-wrap gap-6">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={formState.enabled}
                      onChange={(e) => setFormState((current) => ({ ...current, enabled: e.target.checked }))}
                    />
                    Company Enabled
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={formState.ceoEnabled}
                      onChange={(e) => setFormState((current) => ({ ...current, ceoEnabled: e.target.checked }))}
                    />
                    CEO Enabled
                  </label>
                </div>

                {updatePreview && (
                  <div className="space-y-2">
                    <p className="text-sm font-medium">Apply Preview</p>
                    <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                      {JSON.stringify(updatePreview, null, 2)}
                    </pre>
                  </div>
                )}

                <div className="flex gap-2">
                  <Button type="submit" disabled={updateCompany.isPending}>
                    {updateCompany.isPending ? 'Saving...' : 'Save Changes'}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setFormState({
                        name: company.name,
                        domain: company.domain,
                        enabled: company.enabled,
                        ceoEnabled: company.ceo.enabled,
                        defaultMode: company.preferences.default_mode,
                        budgetHourlyUsd: String(company.preferences.budget_hourly_usd),
                        budgetMonthlyUsd: String(company.preferences.budget_monthly_usd),
                      });
                      setFormError(null);
                    }}
                  >
                    Reset
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'config' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Preferences</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                {JSON.stringify(company.preferences, null, 2)}
              </pre>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>CEO Config</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                {JSON.stringify(company.ceo, null, 2)}
              </pre>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Routing Bindings</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                {JSON.stringify(company.routing_bindings, null, 2)}
              </pre>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>MCP Servers</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                {JSON.stringify(company.mcp_servers, null, 2)}
              </pre>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'teams' && (
        <>
          {company.teams.length === 0 ? (
            <EmptyState message="No company teams found" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Team ID</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {company.teams.map((teamId) => (
                  <TableRow key={teamId}>
                    <TableCell className="font-mono text-xs">{teamId}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}

      {activeTab === 'jobs' && (
        <>
          {jobsQuery.error && <ErrorAlert message={getErrorMessage(jobsQuery.error)} />}
          {jobs.length === 0 ? (
            <EmptyState message="No company jobs found" />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>ID</TableHead>
                  <TableHead>Task</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell className="font-mono text-xs">
                      <Link to="/console/jobs/$jobId" params={{ jobId: job.id }} className="text-primary hover:underline">
                        {job.id.slice(0, 8)}
                      </Link>
                    </TableCell>
                    <TableCell className="max-w-[420px] truncate" title={job.task}>
                      {job.task}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={job.status} />
                    </TableCell>
                    <TableCell>{formatTimestamp(job.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}

      {activeTab === 'schedules' && (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Configured Company Schedules</CardTitle>
            </CardHeader>
            <CardContent>
              {company.schedules.length === 0 ? (
                <EmptyState message="No configured company schedules" />
              ) : (
                <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                  {JSON.stringify(company.schedules, null, 2)}
                </pre>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Runtime Schedule Status</CardTitle>
            </CardHeader>
            <CardContent>
              {scheduleEntries.length === 0 ? (
                <EmptyState message="No runtime schedule status found" />
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Enabled</TableHead>
                      <TableHead>Interval</TableHead>
                      <TableHead>Last Run</TableHead>
                      <TableHead>Errors</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {scheduleEntries.map(([name, status]) => (
                      <TableRow key={name}>
                        <TableCell className="font-mono text-xs">{name}</TableCell>
                        <TableCell>{status.enabled ? 'yes' : 'no'}</TableCell>
                        <TableCell>{status.interval_seconds}s</TableCell>
                        <TableCell>{formatTimestamp(status.last_run)}</TableCell>
                        <TableCell>{status.error_count}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'finance' && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Hourly Budget</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(company.finance?.hourly_budget_usd)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Monthly Budget</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(company.finance?.monthly_budget_usd)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Last Hour Spend</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(company.finance?.last_hour_spend)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm text-muted-foreground">Last 24h Spend</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{formatDollars(company.finance?.last_24h_spend)}</p>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'bindings' && (
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Current Bindings</CardTitle>
            </CardHeader>
            <CardContent>
              {company.bindings.length === 0 ? (
                <EmptyState message="No company bindings found" />
              ) : (
                <div className="space-y-3">
                  {company.bindings.map((binding, index) => (
                    <pre key={`${binding.channel ?? 'any'}-${binding.chat_id ?? 'chat'}-${index}`} className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
                      {JSON.stringify(binding, null, 2)}
                    </pre>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Add Binding</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {bindingError && <ErrorAlert message={bindingError} />}
              {bindingMessage && (
                <p className={`text-sm ${bindingMessage.includes('failed') ? 'text-destructive' : 'text-green-600'}`}>
                  {bindingMessage}
                </p>
              )}
              <form className="grid grid-cols-1 md:grid-cols-3 gap-4" onSubmit={handleAddBinding}>
                <div className="space-y-2">
                  <Label htmlFor="binding-channel">Channel</Label>
                  <Input
                    id="binding-channel"
                    value={bindingForm.channel}
                    onChange={(e) => setBindingForm((current) => ({ ...current, channel: e.target.value }))}
                    placeholder="telegram"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="binding-chat-id">Chat ID</Label>
                  <Input
                    id="binding-chat-id"
                    value={bindingForm.chat_id}
                    onChange={(e) => setBindingForm((current) => ({ ...current, chat_id: e.target.value }))}
                    placeholder="123456"
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="binding-user-id">User ID</Label>
                  <Input
                    id="binding-user-id"
                    value={bindingForm.user_id}
                    onChange={(e) => setBindingForm((current) => ({ ...current, user_id: e.target.value }))}
                    placeholder="operator-1"
                  />
                </div>
                <div className="md:col-span-3">
                  <Button type="submit" disabled={addBinding.isPending}>
                    {addBinding.isPending ? 'Adding...' : 'Add Binding'}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>
      )}

      {activeTab === 'raw' && (
        <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
          {JSON.stringify(company, null, 2)}
        </pre>
      )}

      {showDelete && (
        <DeleteCompanyDialog
          companyId={company.id}
          companyName={company.name}
          onClose={() => setShowDelete(false)}
          onDelete={handleDeleteCompany}
          isPending={deleteCompany.isPending}
          error={deleteCompany.error ? getErrorMessage(deleteCompany.error) : undefined}
        />
      )}
    </div>
  );
}

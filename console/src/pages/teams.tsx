import { useState } from 'react';
import { useTeams } from '@/lib/queries';
import { useRunTeamTask } from '@/lib/mutations';
import { getErrorMessage, getResponseNote } from '@/lib/api';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

const MODE_OPTIONS = ['default', 'cascade', 'loop', 'council', 'collaborative', 'conditional'] as const;

function RunTeamDialog({
  teamId,
  onClose,
}: {
  teamId: string;
  onClose: () => void;
}) {
  const runTeamTask = useRunTeamTask();
  const [task, setTask] = useState('');
  const [mode, setMode] = useState('default');
  const [skipCostCheck, setSkipCostCheck] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);

  function handleRun(e: React.FormEvent) {
    e.preventDefault();
    setMessage(null);
    setResult(null);

    if (!task.trim()) {
      setMessage('Task is required.');
      return;
    }

    runTeamTask.mutate(
      {
        teamId,
        data: {
          task: task.trim(),
          mode,
          skip_cost_check: skipCostCheck,
        },
      },
      {
        onSuccess: (response) => {
          const responseText = typeof response === 'object' && response && 'result' in response
            ? String((response as { result: unknown }).result ?? '')
            : '';
          setMessage('Task sent to team.');
          setResult(responseText || 'No result returned.');
        },
        onError: (error) => {
          setMessage(`Run failed: ${getErrorMessage(error)}`);
        },
      },
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-background border rounded-lg p-6 space-y-4 max-w-2xl w-full mx-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold">Run Task on Team</h3>
            <p className="text-sm text-muted-foreground font-mono">{teamId}</p>
          </div>
          <Button variant="outline" size="sm" onClick={onClose}>Close</Button>
        </div>

        {message && (
          <p className={`text-sm ${message.includes('failed') ? 'text-destructive' : 'text-green-600'}`}>
            {message}
          </p>
        )}

        <form className="space-y-4" onSubmit={handleRun}>
          <div className="space-y-2">
            <Label htmlFor="team-task">Task</Label>
            <textarea
              id="team-task"
              className="w-full min-h-32 font-mono text-sm border rounded-md p-3 bg-background"
              value={task}
              onChange={(e) => setTask(e.target.value)}
              placeholder="Describe the task to run"
            />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label htmlFor="team-mode">Mode</Label>
              <select
                id="team-mode"
                className="w-full h-10 border rounded-md px-3 bg-background text-sm"
                value={mode}
                onChange={(e) => setMode(e.target.value)}
              >
                {MODE_OPTIONS.map((option) => (
                  <option key={option} value={option}>{option}</option>
                ))}
              </select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="team-mode-preview">Mode Preview</Label>
              <Input id="team-mode-preview" value={mode} readOnly />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={skipCostCheck}
              onChange={(e) => setSkipCostCheck(e.target.checked)}
            />
            Skip cost check
          </label>

          <Button type="submit" disabled={runTeamTask.isPending}>
            {runTeamTask.isPending ? 'Running...' : 'Run Task'}
          </Button>
        </form>

        {result && (
          <div className="space-y-2">
            <p className="text-sm font-medium">Result</p>
            <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap max-h-64 overflow-auto">
              {result}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

export function TeamsPage() {
  const { data, isLoading, error } = useTeams();
  const [runningTeamId, setRunningTeamId] = useState<string | null>(null);

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={getErrorMessage(error)} />;

  const teamEntries = data ? Object.entries(data.configs) : [];
  const activeSet = new Set(data?.active ?? []);
  const note = getResponseNote(data);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Teams ({teamEntries.length})</h2>
        {data && <JsonDrawer title="Teams Response" data={data} />}
      </div>

      {teamEntries.length === 0 ? (
        <EmptyState message={note ?? 'No teams found'} />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Lead Pi</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead>Always On</TableHead>
              <TableHead>Active</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {teamEntries.map(([id, config]) => (
              <TableRow key={id}>
                <TableCell className="font-mono text-xs">{id}</TableCell>
                <TableCell>{config.role}</TableCell>
                <TableCell>{config.lead_pi}</TableCell>
                <TableCell>{config.mode}</TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={config.always_on ? 'bg-blue-100 text-blue-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {config.always_on ? 'yes' : 'no'}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Badge
                    variant="outline"
                    className={activeSet.has(id) ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-800'}
                  >
                    {activeSet.has(id) ? 'active' : 'inactive'}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Button size="sm" variant="outline" onClick={() => setRunningTeamId(id)}>
                    Run Task
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {runningTeamId && (
        <RunTeamDialog teamId={runningTeamId} onClose={() => setRunningTeamId(null)} />
      )}
    </div>
  );
}

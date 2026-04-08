import { Link } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { useEvents } from '@/lib/queries';
import { getErrorMessage, getResponseNote } from '@/lib/api';
import { useRealtime } from '@/lib/realtime';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type { ConsoleEvent } from '@/lib/types';

const PAGE_SIZE = 25;

function summarizeEvent(event: ConsoleEvent): string {
  const payload = event.payload;
  const preferredKeys = [
    'task_preview',
    'task',
    'reason',
    'error',
    'name',
    'task_name',
  ] as const;

  for (const key of preferredKeys) {
    const value = payload[key];
    if (typeof value === 'string' && value.trim()) {
      return value.length > 120 ? `${value.slice(0, 120)}...` : value;
    }
  }

  if (typeof payload.team_id === 'string') {
    return `Team ${payload.team_id}`;
  }
  if (typeof payload.job_id === 'string') {
    return `Job ${payload.job_id}`;
  }
  if (Array.isArray(payload.selected_teams) && payload.selected_teams.length > 0) {
    return `Selected ${payload.selected_teams.join(', ')}`;
  }

  const compact = JSON.stringify(payload);
  return compact.length > 120 ? `${compact.slice(0, 120)}...` : compact;
}

export function EventsPage() {
  const { getEventsUnreadCount, isLiveEvent, markEventsSeen } = useRealtime();
  const [typeFilter, setTypeFilter] = useState('');
  const [companyFilter, setCompanyFilter] = useState('');
  const [timeRange, setTimeRange] = useState('24');
  const [offset, setOffset] = useState(0);

  useEffect(() => (
    () => {
      markEventsSeen();
    }
  ), [markEventsSeen]);

  const { data, isLoading, error } = useEvents({
    event_type: typeFilter.trim() || undefined,
    company_id: companyFilter.trim() || undefined,
    hours: timeRange === 'all' ? undefined : Number(timeRange),
    limit: PAGE_SIZE,
    offset,
  });

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={getErrorMessage(error)} />;
  if (!data) return <ErrorAlert message="No event data available" />;

  const events = data.events ?? [];
  const total = data.total ?? 0;
  const note = getResponseNote(data);
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;
  const unreadCount = getEventsUnreadCount();

  return (
    <div className="p-6 space-y-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="space-y-1">
          <div className="flex items-center gap-3">
            <h2 className="text-2xl font-semibold">Events</h2>
            {unreadCount > 0 && (
              <Badge variant="outline" className="bg-amber-100 text-amber-900">
                {unreadCount} new
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Historical event timeline with REST fallback and realtime updates.
          </p>
        </div>
        <JsonDrawer title="Events Response" data={data} />
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Type</p>
          <Input
            value={typeFilter}
            onChange={(e) => {
              setTypeFilter(e.target.value);
              setOffset(0);
            }}
            placeholder="task_completed"
          />
        </div>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Company ID</p>
          <Input
            value={companyFilter}
            onChange={(e) => {
              setCompanyFilter(e.target.value);
              setOffset(0);
            }}
            placeholder="prediction-market"
          />
        </div>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Time Range</p>
          <select
            className="h-10 w-full rounded-md border bg-background px-3 text-sm"
            value={timeRange}
            onChange={(e) => {
              setTimeRange(e.target.value);
              setOffset(0);
            }}
          >
            <option value="1">Last hour</option>
            <option value="24">Last 24 hours</option>
            <option value="168">Last 7 days</option>
            <option value="all">All available</option>
          </select>
        </div>
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">Latest Sequence</p>
          <div className="flex h-10 items-center rounded-md border bg-muted px-3 font-mono text-sm">
            {data.latest_sequence_id}
          </div>
        </div>
      </div>

      {events.length === 0 ? (
        <EmptyState message={note ?? 'No events matched these filters'} />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Seq</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Timestamp</TableHead>
                <TableHead>Company</TableHead>
                <TableHead>Summary</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((event) => (
                <TableRow
                  key={event.sequence_id}
                  className={isLiveEvent(event.sequence_id) ? 'bg-emerald-50/50' : undefined}
                >
                  <TableCell className="font-mono text-xs">{event.sequence_id}</TableCell>
                  <TableCell>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline">{event.type}</Badge>
                      {isLiveEvent(event.sequence_id) && (
                        <Badge variant="outline" className="bg-emerald-100 text-emerald-800">
                          live
                        </Badge>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>{new Date(event.timestamp).toLocaleString()}</TableCell>
                  <TableCell>
                    {event.company_id ? (
                      <Link
                        to="/console/companies/$companyId"
                        params={{ companyId: event.company_id }}
                        className="text-primary hover:underline"
                      >
                        {event.company_id}
                      </Link>
                    ) : (
                      '-'
                    )}
                  </TableCell>
                  <TableCell className="max-w-[420px] truncate" title={summarizeEvent(event)}>
                    {summarizeEvent(event)}
                  </TableCell>
                  <TableCell>
                    <JsonDrawer title={`Event ${event.sequence_id}`} data={event} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              Showing {offset + 1}-{Math.min(offset + PAGE_SIZE, total)} of {total}
            </p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!hasPrev}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!hasNext}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Next
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* eslint-disable react-refresh/only-export-components, react-hooks/set-state-in-effect */
import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  useCallback,
  type ReactNode,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { RefreshCw, Wifi, WifiOff } from 'lucide-react';
import { apiFetch, getEventsWebSocketUrl, getToken } from './api';
import { useMeta } from './queries';
import type { ConsoleEvent, EventsResponse } from './types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

type ConnectionStatus = 'loading' | 'disabled' | 'connecting' | 'connected' | 'reconnecting' | 'error';

interface RealtimeContextValue {
  connectionStatus: ConnectionStatus;
  eventsHistoryEnabled: boolean;
  websocketEnabled: boolean;
  lastRefreshAt: number;
  lastSequenceId: number;
  recentEvents: ConsoleEvent[];
  isStale: boolean;
  refreshAll: () => Promise<void>;
  isLiveEvent: (sequenceId: number) => boolean;
  getOverviewCompanyActivityCount: (companyId: string) => number;
  getCompanyActivityCount: (companyId: string) => number;
  getEventsUnreadCount: () => number;
  markOverviewSeen: () => void;
  markCompanySeen: (companyId: string) => void;
  markEventsSeen: () => void;
}

const RECENT_EVENTS_LIMIT = 200;
const BACKGROUND_REFRESH_MS = 60_000;
const STALE_AFTER_MS = 90_000;
const seenPrefix = 'companest_seen_seq:';

const RealtimeContext = createContext<RealtimeContextValue | null>(null);

function readSeenSequence(scope: string): number {
  const raw = sessionStorage.getItem(`${seenPrefix}${scope}`);
  const parsed = Number(raw ?? '0');
  return Number.isFinite(parsed) ? parsed : 0;
}

function writeSeenSequence(scope: string, sequenceId: number): void {
  sessionStorage.setItem(`${seenPrefix}${scope}`, String(sequenceId));
}

function mergeEvents(existing: ConsoleEvent[], incoming: ConsoleEvent[]): ConsoleEvent[] {
  const map = new Map<number, ConsoleEvent>();
  for (const event of [...incoming, ...existing]) {
    map.set(event.sequence_id, event);
  }
  return Array.from(map.values())
    .sort((a, b) => b.sequence_id - a.sequence_id)
    .slice(0, RECENT_EVENTS_LIMIT);
}

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const meta = useMeta();
  const queryClient = useQueryClient();
  const [socketStatus, setSocketStatus] = useState<ConnectionStatus>('connecting');
  const [recentEvents, setRecentEvents] = useState<ConsoleEvent[]>([]);
  const [liveEventIds, setLiveEventIds] = useState<number[]>([]);
  const [lastSequenceId, setLastSequenceId] = useState(0);
  const [lastRefreshAt, setLastRefreshAt] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const lastSequenceIdRef = useRef(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const refreshTimerRef = useRef<number | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const hiddenAtRef = useRef<number | null>(null);

  const websocketEnabled = meta.data?.capabilities.websocket ?? false;
  const eventsHistoryEnabled = meta.data?.capabilities.events_history ?? false;
  const connectionStatus: ConnectionStatus = meta.isLoading
    ? 'loading'
    : meta.error
      ? 'error'
      : !websocketEnabled
        ? 'disabled'
        : socketStatus;

  useEffect(() => {
    lastSequenceIdRef.current = lastSequenceId;
  }, [lastSequenceId]);

  const loadRecentEvents = useCallback(async () => {
    if (!eventsHistoryEnabled) {
      setRecentEvents([]);
      return;
    }
    const response = await apiFetch<EventsResponse>(`/events?limit=${RECENT_EVENTS_LIMIT}`);
    setRecentEvents(response.events);
    setLastSequenceId(response.latest_sequence_id);
    setLastRefreshAt(Date.now());
  }, [eventsHistoryEnabled]);

  const refreshAll = useCallback(async () => {
    await queryClient.invalidateQueries();
    if (eventsHistoryEnabled) {
      await loadRecentEvents();
    } else {
      setLastRefreshAt(Date.now());
    }
  }, [eventsHistoryEnabled, loadRecentEvents, queryClient]);

  useEffect(() => {
    if (meta.isLoading || meta.error) return;
    if (!eventsHistoryEnabled) return;
    void loadRecentEvents();
  }, [eventsHistoryEnabled, loadRecentEvents, meta.error, meta.isLoading]);

  useEffect(() => {
    if (!websocketEnabled || meta.isLoading || meta.error) return;

    let cancelled = false;

    function clearReconnectTimer() {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    }

    function scheduleRefresh() {
      if (refreshTimerRef.current !== null) return;
      refreshTimerRef.current = window.setTimeout(() => {
        refreshTimerRef.current = null;
        void refreshAll();
      }, 800);
    }

    function connect() {
      if (cancelled) return;
      setSocketStatus(reconnectAttemptsRef.current > 0 ? 'reconnecting' : 'connecting');
      const ws = new WebSocket(getEventsWebSocketUrl());
      wsRef.current = ws;

        ws.onopen = () => {
          reconnectAttemptsRef.current = 0;
          ws.send(JSON.stringify({
            type: 'auth',
            token: getToken(),
            last_sequence_id: lastSequenceIdRef.current,
          }));
        };

      ws.onmessage = (message) => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(message.data);
        } catch {
          return;
        }
        if (!parsed || typeof parsed !== 'object') return;
        const event = parsed as Record<string, unknown>;
        const type = typeof event.type === 'string' ? event.type : '';

        if (type === 'ready') {
          setSocketStatus('connected');
          const sequenceId = Number(event.sequence_id ?? lastSequenceIdRef.current);
          if (Number.isFinite(sequenceId)) {
            setLastSequenceId(sequenceId);
          }
          if (event.missed_events === true) {
            void refreshAll();
          }
          return;
        }

        if (type === 'ping') {
          const sequenceId = Number(event.sequence_id ?? lastSequenceIdRef.current);
          if (Number.isFinite(sequenceId)) {
            setLastSequenceId(sequenceId);
          }
          return;
        }

        const sequenceId = Number(event.sequence_id);
        if (!Number.isFinite(sequenceId)) return;

        const realtimeEvent: ConsoleEvent = {
          sequence_id: sequenceId,
          type,
          timestamp: typeof event.timestamp === 'string' ? event.timestamp : new Date().toISOString(),
          company_id: typeof event.company_id === 'string' ? event.company_id : null,
          payload: event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload)
            ? event.payload as Record<string, unknown>
            : {},
        };

        setLastSequenceId(sequenceId);
        setLastRefreshAt(Date.now());
        setRecentEvents((current) => mergeEvents(current, [realtimeEvent]));
        setLiveEventIds((current) => [sequenceId, ...current.filter((id) => id !== sequenceId)].slice(0, RECENT_EVENTS_LIMIT));
        scheduleRefresh();
      };

      ws.onerror = () => {
        setSocketStatus('error');
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (cancelled) return;
        setSocketStatus('reconnecting');
        const nextDelay = Math.min(30_000, 1000 * (2 ** reconnectAttemptsRef.current));
        reconnectAttemptsRef.current += 1;
        clearReconnectTimer();
        reconnectTimerRef.current = window.setTimeout(connect, nextDelay);
      };
    }

    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      if (refreshTimerRef.current !== null) {
        window.clearTimeout(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [meta.error, meta.isLoading, refreshAll, websocketEnabled]);

  useEffect(() => {
    function handleVisibilityChange() {
      if (document.visibilityState === 'hidden') {
        hiddenAtRef.current = Date.now();
        return;
      }
      const hiddenAt = hiddenAtRef.current;
      hiddenAtRef.current = null;
      if (hiddenAt && Date.now() - hiddenAt > BACKGROUND_REFRESH_MS) {
        void refreshAll();
      }
    }

    function handleFocus() {
      const hiddenAt = hiddenAtRef.current;
      if (hiddenAt && Date.now() - hiddenAt > BACKGROUND_REFRESH_MS) {
        hiddenAtRef.current = null;
        void refreshAll();
      }
    }

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('focus', handleFocus);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', handleFocus);
    };
  }, [refreshAll]);

  const isStale = websocketEnabled && connectionStatus !== 'connected';

  const countSince = useCallback((scope: string, companyId?: string): number => {
    const seenSequence = readSeenSequence(scope);
    return recentEvents.filter((event) => (
      event.sequence_id > seenSequence
      && (!companyId || event.company_id === companyId)
    )).length;
  }, [recentEvents]);

  const isLiveEvent = useCallback((sequenceId: number): boolean => (
    liveEventIds.includes(sequenceId)
  ), [liveEventIds]);

  const markOverviewSeen = useCallback(() => {
    writeSeenSequence('overview', lastSequenceIdRef.current);
  }, []);

  const markCompanySeen = useCallback((companyId: string) => {
    writeSeenSequence(`company:${companyId}`, lastSequenceIdRef.current);
  }, []);

  const markEventsSeen = useCallback(() => {
    writeSeenSequence('events', lastSequenceIdRef.current);
  }, []);

  const value: RealtimeContextValue = {
    connectionStatus,
    eventsHistoryEnabled,
    websocketEnabled,
    lastRefreshAt,
    lastSequenceId,
    recentEvents,
    isStale,
    refreshAll,
    isLiveEvent,
    getOverviewCompanyActivityCount: (companyId: string) => countSince('overview', companyId),
    getCompanyActivityCount: (companyId: string) => countSince(`company:${companyId}`, companyId),
    getEventsUnreadCount: () => countSince('events'),
    markOverviewSeen,
    markCompanySeen,
    markEventsSeen,
  };

  return (
    <RealtimeContext.Provider value={value}>
      {children}
    </RealtimeContext.Provider>
  );
}

export function useRealtime() {
  const context = useContext(RealtimeContext);
  if (!context) {
    throw new Error('useRealtime must be used inside RealtimeProvider');
  }
  return context;
}

function getStatusLabel(status: ConnectionStatus, websocketEnabled: boolean): string {
  if (!websocketEnabled) return 'REST only';
  switch (status) {
    case 'connected':
      return 'Live';
    case 'reconnecting':
      return 'Reconnecting';
    case 'connecting':
      return 'Connecting';
    case 'error':
      return 'Error';
    case 'disabled':
      return 'Disabled';
    default:
      return 'Loading';
  }
}

export function RealtimeStatusBar() {
  const { connectionStatus, websocketEnabled, lastRefreshAt, refreshAll, lastSequenceId } = useRealtime();
  const [now, setNow] = useState(lastRefreshAt);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setNow(lastRefreshAt);
  }, [lastRefreshAt]);

  const secondsAgo = Math.max(0, Math.floor((now - lastRefreshAt) / 1000));
  const isStale = now - lastRefreshAt > STALE_AFTER_MS || (websocketEnabled && connectionStatus !== 'connected');

  return (
    <div className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 border-b bg-background/95 px-6 py-3 backdrop-blur">
      <div className="flex items-center gap-3 text-sm">
        <Badge
          variant="outline"
          className={connectionStatus === 'connected'
            ? 'bg-green-100 text-green-800'
            : isStale
              ? 'bg-amber-100 text-amber-900'
              : 'bg-gray-100 text-gray-800'}
        >
          {websocketEnabled && connectionStatus === 'connected' ? <Wifi className="mr-1 h-3 w-3" /> : <WifiOff className="mr-1 h-3 w-3" />}
          {getStatusLabel(connectionStatus, websocketEnabled)}
        </Badge>
        <span className="text-muted-foreground">
          Last updated {secondsAgo}s ago
        </span>
        <span className="text-muted-foreground font-mono text-xs">
          seq {lastSequenceId}
        </span>
        {isStale && (
          <span className="text-amber-700 text-xs font-medium">
            State may be stale
          </span>
        )}
      </div>

      <Button variant="outline" size="sm" onClick={() => void refreshAll()}>
        <RefreshCw className="mr-1 h-4 w-4" />
        Refresh
      </Button>
    </div>
  );
}

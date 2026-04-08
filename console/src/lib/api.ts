const API_BASE = import.meta.env.VITE_API_BASE || '/api';
const LOGIN_PATH = '/console/login';
const WS_BASE = import.meta.env.VITE_WS_BASE || `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;

class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

function getToken(): string | null {
  return sessionStorage.getItem('companest_token');
}

function setToken(token: string): void {
  sessionStorage.setItem('companest_token', token);
}

function clearToken(): void {
  sessionStorage.removeItem('companest_token');
}

function isObjectRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function getResponseNote(value: unknown): string | null {
  if (!isObjectRecord(value)) return null;
  return typeof value.note === 'string' && value.note.trim() ? value.note : null;
}

function getEventsWebSocketUrl(): string {
  const trimmed = WS_BASE.replace(/\/+$/, '');
  if (trimmed.startsWith('https://')) {
    return `${trimmed.replace('https://', 'wss://')}/ws/events`;
  }
  if (trimmed.startsWith('http://')) {
    return `${trimmed.replace('http://', 'ws://')}/ws/events`;
  }
  if (trimmed.startsWith('ws://') || trimmed.startsWith('wss://')) {
    return `${trimmed}/ws/events`;
  }
  const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
  return `${protocol}${trimmed}/ws/events`;
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options?.headers as Record<string, string>),
  };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (res.status === 401) {
    clearToken();
    window.location.href = LOGIN_PATH;
    throw new ApiError(401, 'Unauthorized');
  }

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = isObjectRecord(body)
      ? String(body.detail || body.error || `HTTP ${res.status}`)
      : `HTTP ${res.status}`;
    throw new ApiError(res.status, detail);
  }

  return res.json();
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  });
}

async function apiDelete<T>(path: string): Promise<T> {
  return apiFetch<T>(path, { method: 'DELETE' });
}

async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export {
  apiFetch,
  apiPost,
  apiDelete,
  apiPut,
  apiPatch,
  getToken,
  setToken,
  clearToken,
  getErrorMessage,
  getEventsWebSocketUrl,
  getResponseNote,
  ApiError,
};

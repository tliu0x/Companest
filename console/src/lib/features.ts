const rawTopologyFlag = String(import.meta.env.VITE_ENABLE_TOPOLOGY ?? 'true').toLowerCase();

export const TOPOLOGY_ENABLED = rawTopologyFlag !== '0' && rawTopologyFlag !== 'false';

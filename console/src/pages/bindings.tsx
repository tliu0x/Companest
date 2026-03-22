import { useState, useEffect } from 'react';
import { useBindings } from '@/lib/queries';
import { useSetBindings } from '@/lib/mutations';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';

export function BindingsPage() {
  const { data, isLoading, error } = useBindings();
  const setBindings = useSetBindings();

  const [editJson, setEditJson] = useState('');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const bindings = data?.bindings ?? [];

  useEffect(() => {
    if (data) {
      setEditJson(JSON.stringify(data.bindings ?? [], null, 2));
    }
  }, [data]);

  function handleSave() {
    try {
      const parsed = JSON.parse(editJson);
      if (!Array.isArray(parsed)) {
        setJsonError('Bindings must be a JSON array');
        return;
      }
      if (parsed.some((item: unknown) => item === null || typeof item !== 'object' || Array.isArray(item))) {
        setJsonError('Each binding must be a non-null object');
        return;
      }
      setJsonError(null);
      setBindings.mutate(parsed, {
        onSuccess: () => setEditing(false),
      });
    } catch {
      setJsonError('Invalid JSON');
    }
  }

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Bindings ({bindings.length})</h2>
        <div className="flex gap-2">
          {data && <JsonDrawer title="Bindings Response" data={data} />}
          <Button
            variant={editing ? 'outline' : 'default'}
            onClick={() => {
              setEditing(!editing);
              setJsonError(null);
            }}
          >
            {editing ? 'Cancel Edit' : 'Edit Bindings'}
          </Button>
        </div>
      </div>

      {editing && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Edit Bindings (JSON)</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {jsonError && <ErrorAlert message={jsonError} />}
            {setBindings.error && (
              <ErrorAlert message={(setBindings.error as Error).message} />
            )}
            <textarea
              className="w-full h-80 font-mono text-xs border rounded-md p-3 bg-muted"
              value={editJson}
              onChange={(e) => setEditJson(e.target.value)}
            />
            <Button onClick={handleSave} disabled={setBindings.isPending}>
              {setBindings.isPending ? 'Saving...' : 'Save Bindings'}
            </Button>
          </CardContent>
        </Card>
      )}

      {!editing && (
        <>
          {bindings.length === 0 ? (
            <EmptyState message="No bindings found" />
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {bindings.map((binding, i) => (
                <Card key={i}>
                  <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-mono">
                      {binding.team_id} ({binding.mode})
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <pre className="text-xs font-mono bg-muted p-3 rounded-md whitespace-pre-wrap overflow-auto max-h-[200px]">
                      {JSON.stringify(binding, null, 2)}
                    </pre>
                  </CardContent>
                </Card>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

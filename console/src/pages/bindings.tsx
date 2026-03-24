import { useBindings } from '@/lib/queries';
import { PageLoading } from '@/components/shared/loading';
import { ErrorAlert } from '@/components/shared/error-alert';
import { EmptyState } from '@/components/shared/empty-state';
import { JsonDrawer } from '@/components/shared/json-drawer';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function BindingsPage() {
  const { data, isLoading, error } = useBindings();

  if (isLoading) return <PageLoading />;
  if (error) return <ErrorAlert message={error.message} />;

  const bindings = data ?? [];

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-semibold">Bindings ({bindings.length})</h2>
        {data && <JsonDrawer title="Bindings Response" data={data} />}
      </div>

      {bindings.length === 0 ? (
        <EmptyState message="No bindings found" />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {bindings.map((binding, i) => (
            <Card key={i}>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm font-mono">
                  {(binding.name as string) ?? (binding.id as string) ?? `Binding ${i + 1}`}
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
    </div>
  );
}

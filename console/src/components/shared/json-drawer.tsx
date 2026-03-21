import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Code } from 'lucide-react';

export function JsonDrawer({ title, data }: { title: string; data: unknown }) {
  return (
    <Sheet>
      <SheetTrigger render={<Button variant="outline" size="sm" />}>
        <Code className="h-4 w-4 mr-1" />
        JSON
      </SheetTrigger>
      <SheetContent className="w-[500px] sm:max-w-[500px]">
        <SheetHeader>
          <SheetTitle>{title}</SheetTitle>
        </SheetHeader>
        <ScrollArea className="h-[calc(100vh-100px)] mt-4">
          <pre className="text-xs font-mono bg-muted p-4 rounded-md whitespace-pre-wrap">
            {JSON.stringify(data, null, 2)}
          </pre>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}

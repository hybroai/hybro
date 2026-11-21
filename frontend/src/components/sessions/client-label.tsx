import { cn } from '@/lib/utils'

/** The name a program gave itself, shown beside its client; nothing verifies it. */
export function ClientLabel({ label, className }: { label: string; className?: string }) {
  return (
    <span title="Name the client gave; not verified" className={cn('truncate text-xs text-muted-foreground italic', className)}>
      {`“${label}”`}
    </span>
  )
}

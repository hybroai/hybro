export function escapeCssIdent(value: string): string {
  return typeof CSS !== 'undefined' && CSS.escape
    ? CSS.escape(value)
    : value.replace(/"/g, '\\"')
}

import {
  Type,
  Image as ImageIcon,
  Video,
  Music,
  Braces,
  FileText,
  SquareCode,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"

export const MIME_ICON_MAP: [RegExp, LucideIcon][] = [
  [/^text\//, Type],
  [/^image\//, ImageIcon],
  [/^video\//, Video],
  [/^audio\//, Music],
  [/^application\/json$/, Braces],
  [/^application\/pdf$/, FileText],
]

export function getModeIcon(mime: string): LucideIcon {
  for (const [pattern, icon] of MIME_ICON_MAP) {
    if (pattern.test(mime)) return icon
  }
  return SquareCode
}

const MIME_LABEL_MAP: [RegExp, string][] = [
  [/^text\/plain$/, "Text"],
  [/^text\/html$/, "HTML"],
  [/^text\/markdown$/, "Markdown"],
  [/^text\/csv$/, "CSV"],
  [/^text\//, "Text"],
  [/^image\/png$/, "Images"],
  [/^image\/jpeg$/, "Images"],
  [/^image\/gif$/, "Images"],
  [/^image\/webp$/, "Images"],
  [/^image\/svg\+xml$/, "SVG"],
  [/^image\//, "Images"],
  [/^video\//, "Video"],
  [/^audio\//, "Audio"],
  [/^application\/json$/, "Structured Data"],
  [/^application\/pdf$/, "PDF Documents"],
  [/^application\/xml$/, "XML"],
  [/^application\/zip$/, "Archives"],
]

export function getModeLabel(mime: string): string {
  for (const [pattern, label] of MIME_LABEL_MAP) {
    if (pattern.test(mime)) return label
  }
  return mime
}

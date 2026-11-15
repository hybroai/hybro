/**
 * Shared sidebar collapsed-mode (icon-only) style constants.
 *
 * These keep the `group-data-[collapsible=icon]:*` classes in one place
 * so that sizing stays consistent across all sidebar nav components and
 * the "icon vertical shift on toggle" bug cannot silently regress.
 */

/** Button-level: centers content, removes horizontal padding, matches expanded height. */
export const SIDEBAR_ICON_BUTTON =
  "group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0 group-data-[collapsible=icon]:h-10! group-data-[collapsible=icon]:w-full!"

/** Icon-level: centers the icon horizontally inside its collapsed button. */
export const SIDEBAR_ICON_CENTER = "group-data-[collapsible=icon]:mx-auto"

/** Text / label: hidden when the sidebar is collapsed to icon-only. */
export const SIDEBAR_ICON_HIDDEN = "group-data-[collapsible=icon]:hidden"

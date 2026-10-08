// Phase 6A — DataTable.
//
// Compact enterprise table patterned after the HipLink Security
// Operations reference.  Columns are declarative; rows are
// supplied as plain objects.  Empty / loading states are
// components the caller passes so the table can blend visually
// with the page-level EmptyState / LoadingSkeleton.

import type { ReactNode } from 'react'

export interface DataTableColumn<T> {
  key: string
  header: ReactNode
  /** Cell renderer; receives the row and a `value` (the row's `valueFor` result). */
  cell: (row: T, value: unknown) => ReactNode
  /** Optional accessor — defaults to `row[key]`. */
  valueFor?: (row: T) => unknown
  /** Tailwind width class, e.g. 'w-32'. */
  width?: string
  /** Right-align numeric columns. */
  numeric?: boolean
  /** Hide on small screens. */
  hideOnMobile?: boolean
}

export interface DataTableProps<T> {
  columns: DataTableColumn<T>[]
  rows: T[]
  /** Stable key per row. */
  rowKey: (row: T) => string | number
  /** Total row count when paginated (optional). */
  totalCount?: number
  isLoading?: boolean
  emptyState?: ReactNode
  caption?: string
  /** When true the table becomes horizontally scrollable on small screens. */
  scrollable?: boolean
  /** Optional row click handler. */
  onRowClick?: (row: T) => void
  /** Predicate — return false to make the row non-interactive. */
  isRowClickable?: (row: T) => boolean
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  isLoading = false,
  emptyState,
  caption,
  scrollable = true,
  onRowClick,
  isRowClickable,
}: DataTableProps<T>) {
  const table = (
    <table className="min-w-full text-sm">
      {caption && <caption className="sr-only">{caption}</caption>}
      <thead className="bg-surface-2 text-xs uppercase tracking-wider text-fg-muted">
        <tr>
          {columns.map((col) => (
            <th
              key={col.key}
              scope="col"
              className={[
                'px-3 py-2 font-medium',
                col.numeric ? 'text-right' : 'text-left',
                col.hideOnMobile ? 'hidden sm:table-cell' : '',
                col.width ?? '',
              ].join(' ')}
            >
              {col.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {isLoading ? (
          <tr>
            <td colSpan={columns.length} className="px-3 py-6 text-center text-fg-muted">
              Loading…
            </td>
          </tr>
        ) : rows.length === 0 ? (
          <tr>
            <td colSpan={columns.length} className="px-3 py-6">
              {emptyState ?? (
                <p className="text-center text-fg-muted">No data</p>
              )}
            </td>
          </tr>
        ) : (
          rows.map((row) => {
            const clickable = !!(onRowClick && (isRowClickable ? isRowClickable(row) : true))
            return (
              <tr
                key={rowKey(row)}
                onClick={clickable ? () => onRowClick?.(row) : undefined}
                tabIndex={clickable ? 0 : -1}
                onKeyDown={
                  clickable
                    ? (e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onRowClick?.(row)
                        }
                      }
                    : undefined
                }
                className={[
                  'text-fg-primary transition-colors',
                  clickable ? 'cursor-pointer hover:bg-surface-hover focus-visible:bg-surface-hover' : '',
                ].join(' ')}
              >
                {columns.map((col) => {
                  const value = col.valueFor ? col.valueFor(row) : (row as Record<string, unknown>)[col.key]
                  return (
                    <td
                      key={col.key}
                      className={[
                        'px-3 py-2 align-middle',
                        col.numeric ? 'text-right tabular-nums' : 'text-left',
                        col.hideOnMobile ? 'hidden sm:table-cell' : '',
                      ].join(' ')}
                    >
                      {col.cell(row, value)}
                    </td>
                  )
                })}
              </tr>
            )
          })
        )}
      </tbody>
    </table>
  )

  if (!scrollable) return <div className="overflow-hidden rounded-lg border border-border">{table}</div>
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      {table}
    </div>
  )
}

"use client";

import { useMemo, useState } from "react";
import type { Key, KeyboardEvent, ReactNode } from "react";

export type SortDirection = "asc" | "desc";

export interface TableColumn<T> {
  id?: string;
  key?: keyof T;
  header: ReactNode;
  accessor?: (row: T) => ReactNode;
  sortValue?: (row: T) => string | number | null | undefined;
  sortable?: boolean;
  truncate?: boolean;
  className?: string;
  headerClassName?: string;
  cellClassName?: string;
  align?: "left" | "right";
}

export interface TableSort {
  columnId: string;
  direction: SortDirection;
}

export interface TableProps<T> {
  data: T[];
  columns: TableColumn<T>[];
  emptyMessage?: string;
  getRowKey?: (row: T, index: number) => Key;
  onRowClick?: (row: T) => void;
  initialSort?: TableSort;
  rowClassName?: (row: T, index: number) => string;
}

function columnId<T>(column: TableColumn<T>) {
  return column.id ?? String(column.key ?? column.header);
}

function rawValue<T>(row: T, column: TableColumn<T>): unknown {
  if (!column.key) {
    return undefined;
  }

  return row[column.key];
}

function sortValue<T>(row: T, column: TableColumn<T>) {
  if (column.sortValue) {
    return column.sortValue(row);
  }

  const value = rawValue(row, column);
  return typeof value === "string" || typeof value === "number" ? value : undefined;
}

function compareValues(
  left: string | number | null | undefined,
  right: string | number | null | undefined,
) {
  if (left === right) {
    return 0;
  }

  if (left === null || left === undefined) {
    return 1;
  }

  if (right === null || right === undefined) {
    return -1;
  }

  if (typeof left === "number" && typeof right === "number") {
    return left - right;
  }

  return String(left).localeCompare(String(right));
}

function defaultRowKey<T>(row: T, index: number): Key {
  const candidate = (row as { id?: Key }).id;
  return candidate ?? index;
}

function titleFor(value: ReactNode) {
  if (typeof value === "string" || typeof value === "number") {
    return String(value);
  }

  return undefined;
}

export default function Table<T>({
  data,
  columns,
  emptyMessage = "No rows recorded.",
  getRowKey = defaultRowKey,
  onRowClick,
  initialSort,
  rowClassName,
}: TableProps<T>) {
  const [sort, setSort] = useState<TableSort | null>(initialSort ?? null);

  const sortedData = useMemo(() => {
    if (!sort) {
      return data;
    }

    const column = columns.find((candidate) => columnId(candidate) === sort.columnId);
    if (!column) {
      return data;
    }

    return [...data].sort((left, right) => {
      const result = compareValues(sortValue(left, column), sortValue(right, column));
      return sort.direction === "asc" ? result : -result;
    });
  }, [columns, data, sort]);

  const toggleSort = (column: TableColumn<T>) => {
    const id = columnId(column);

    setSort((current) => {
      if (current?.columnId !== id) {
        return { columnId: id, direction: "asc" };
      }

      return {
        columnId: id,
        direction: current.direction === "asc" ? "desc" : "asc",
      };
    });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, row: T) => {
    if (!onRowClick) {
      return;
    }

    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onRowClick(row);
    }
  };

  return (
    <div className="overflow-x-auto rounded border border-edge bg-surface-raised">
      <table className="min-w-full table-fixed border-collapse text-body">
        <thead className="border-b border-edge bg-surface-overlay text-meta uppercase tracking-[0.06em] text-content-muted">
          <tr>
            {columns.map((column) => {
              const id = columnId(column);
              const active = sort?.columnId === id;
              const sortable = column.sortable !== false;

              return (
                <th
                  key={id}
                  scope="col"
                  className={[
                    "px-3 py-2 font-medium",
                    column.align === "right" ? "text-right" : "text-left",
                    column.className,
                    column.headerClassName,
                  ]
                    .filter(Boolean)
                    .join(" ")}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column)}
                      aria-sort={
                        active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"
                      }
                      className={[
                        "inline-flex max-w-full items-center gap-1 hover:text-content-primary",
                        active ? "text-content-primary" : "text-content-muted",
                      ].join(" ")}
                    >
                      <span className="truncate">{column.header}</span>
                      <span
                        aria-hidden="true"
                        className={[
                          "shrink-0 text-meta tabular-nums",
                          active ? "text-content-primary" : "text-content-muted/60",
                        ].join(" ")}
                      >
                        {active ? (sort.direction === "asc" ? "↑" : "↓") : "↕"}
                      </span>
                    </button>
                  ) : (
                    <span className="truncate">{column.header}</span>
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sortedData.map((row, index) => (
            <tr
              key={getRowKey(row, index)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={(event) => handleKeyDown(event, row)}
              role={onRowClick ? "button" : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              className={[
                "border-b border-edge-subtle text-content-secondary",
                onRowClick ? "cursor-pointer hover:bg-surface-overlay" : "",
                rowClassName?.(row, index),
              ]
                .filter(Boolean)
                .join(" ")}
            >
              {columns.map((column) => {
                const id = columnId(column);
                const value =
                  column.accessor?.(row) ?? ((rawValue(row, column) ?? "—") as ReactNode);
                const shouldTruncate = column.truncate !== false;

                return (
                  <td
                    key={id}
                    className={[
                      "px-3 py-2 align-top",
                      column.align === "right" ? "text-right" : "text-left",
                      column.className,
                      column.cellClassName,
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    <div
                      title={shouldTruncate ? titleFor(value) : undefined}
                      className={shouldTruncate ? "truncate" : undefined}
                    >
                      {value}
                    </div>
                  </td>
                );
              })}
            </tr>
          ))}

          {sortedData.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                className="px-3 py-10 text-center text-body text-content-muted"
              >
                {emptyMessage}
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

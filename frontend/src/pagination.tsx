import { ChevronLeft, ChevronRight } from "lucide-react";

export function fitTableRows(available: number, desktop: boolean) {
  const pageSize = desktop
    ? Math.max(1, Math.min(12, Math.floor(available / 56)))
    : 10;
  return {
    pageSize,
    rowHeight: desktop ? Math.max(56, available / pageSize) : 56,
  };
}

export function Pagination({
  label,
  page,
  pageCount,
  disabled = false,
  onPageChange,
}: {
  label: string;
  page: number;
  pageCount: number;
  disabled?: boolean;
  onPageChange: (page: number) => void;
}) {
  return (
    <nav className="accounts-pagination" aria-label={label}>
      <button
        aria-label="上一页"
        disabled={disabled || page <= 1}
        onClick={() => onPageChange(page - 1)}
      >
        <ChevronLeft size={16} aria-hidden="true" />
      </button>
      <span role="status" aria-live="polite" aria-atomic="true">
        {pageCount ? page : 0} / {pageCount}
      </span>
      <button
        aria-label="下一页"
        disabled={disabled || page >= pageCount}
        onClick={() => onPageChange(page + 1)}
      >
        <ChevronRight size={16} aria-hidden="true" />
      </button>
    </nav>
  );
}

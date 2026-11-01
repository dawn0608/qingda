export function automationConversations<T extends Record<string, any>>(
  rows: T[],
): T[] {
  return rows.filter(
    (row) =>
      (row.kind === "private" || row.kind === "group") &&
      row.verification?.state !== "excluded",
  );
}

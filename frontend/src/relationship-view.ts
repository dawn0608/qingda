type Row = Record<string, any>;
export const relationshipText = (row: Row) =>
  row.removed_at == null
    ? ""
    : row.relationship_reason === "group_kicked"
      ? "已被移出群聊"
      : row.relationship_reason === "group_dissolved"
        ? "群聊已解散"
        : row.kind === "group"
          ? "已退出群聊"
          : "好友已删除";
export const pendingRelationship = (row: Row) =>
  row.removed_at == null &&
  !["verified", "excluded"].includes(row.verification?.state);
export const relationshipBadge = (row: Row) =>
  relationshipText(row) || (pendingRelationship(row) ? "关系待核实" : "");
export const replyText = (row: Row) =>
  ({
    account_disabled: "账号已停用",
    account_offline: "账号离线，暂时无法回复",
    paused: "回复暂停，点击右侧开关恢复",
  })[row.auto_reply_status as string] || "";
export const verificationText = (row: Row) =>
  row.verification?.state === "failed"
    ? "关系查询失败，稍后自动核实"
    : row.verification?.state === "stale"
      ? "关系核实已过期，正在重新核实"
      : "尚无有效关系结果，等待自动核实";

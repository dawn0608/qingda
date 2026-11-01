import test from "node:test";
import assert from "node:assert/strict";
import { automationConversations } from "./automation-scope.ts";

test("自动回复范围保留好友与群聊，按服务端分类排除系统账号而非按昵称", () => {
  const rows = [
    {
      id: "friend",
      kind: "private",
      name: "Dawn",
      allowed: true,
      verification: { state: "verified" },
    },
    {
      id: "same-name",
      kind: "private",
      name: "微信团队",
      allowed: false,
      verification: { state: "verified" },
    },
    {
      id: "pending-friend",
      kind: "private",
      allowed: false,
      verification: { state: "unverified" },
    },
    {
      id: "removed-friend",
      kind: "private",
      removed_at: 123,
      allowed: false,
      verification: { state: "stale" },
    },
    {
      id: "group",
      kind: "group",
      allowed: true,
      verification: { state: "verified" },
    },
    ...["weixin", "filehelper", "fmessage", "gh_public", "self"].map((id) => ({
      id,
      kind: "private",
      allowed: true,
      verification: { state: "excluded" },
    })),
    {
      id: "other-kind",
      kind: "system",
      allowed: true,
      verification: { state: "verified" },
    },
  ];
  const visible = automationConversations(rows);
  assert.deepEqual(
    visible.map((row) => row.id),
    ["friend", "same-name", "pending-friend", "removed-friend", "group"],
  );
  assert.equal(visible.filter((row) => row.allowed).length, 2);
});

test("仅有排除项时列表和已允许数量归零，原始会话及白名单状态不改写", () => {
  const rows = [
    {
      id: "filehelper",
      kind: "private",
      allowed: true,
      verification: { state: "excluded" },
    },
    {
      id: "gh_public",
      kind: "private",
      allowed: false,
      verification: { state: "excluded" },
    },
  ];
  const before = structuredClone(rows);
  const visible = automationConversations(rows);
  assert.deepEqual(visible, []);
  assert.equal(visible.filter((row) => row.allowed).length, 0);
  assert.deepEqual(rows, before);
  assert.deepEqual(automationConversations([]), []);
});

test("切换账号按各自服务端分类处理，同一微信号在另一账号下可以是普通好友", () => {
  const first = [
    {
      id: "first-self",
      peer_id: "wxid_a",
      kind: "private",
      verification: { state: "excluded" },
    },
    {
      id: "first-friend",
      peer_id: "wxid_b",
      kind: "private",
      verification: { state: "verified" },
    },
  ];
  const second = [
    {
      id: "second-friend",
      peer_id: "wxid_a",
      kind: "private",
      verification: { state: "verified" },
    },
    {
      id: "second-self",
      peer_id: "wxid_b",
      kind: "private",
      verification: { state: "excluded" },
    },
  ];
  assert.deepEqual(
    automationConversations(first).map((row) => row.id),
    ["first-friend"],
  );
  assert.deepEqual(
    automationConversations(second).map((row) => row.id),
    ["second-friend"],
  );
  assert.strictEqual(automationConversations(first)[0], first[1]);
});

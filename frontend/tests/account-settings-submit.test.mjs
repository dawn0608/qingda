import assert from "node:assert/strict";
import test from "node:test";
import {
  AccountSettingsSaveError,
  saveProfileSettings,
} from "../src/account-settings-submit.ts";

function setup(overrides = {}) {
  const calls = [];
  const operations = {
    updateUsername: async () => {
      calls.push("name");
    },
    uploadAvatar: async () => {
      calls.push("avatar");
    },
    avatarSaved: () => {
      calls.push("clear-avatar");
    },
    readUser: async () => {
      calls.push("readback");
      return { username: "new" };
    },
    onUser: () => {
      calls.push("refresh");
    },
    ...overrides,
  };
  return { calls, operations };
}

test("unchanged profile makes no write request", async () => {
  const { calls, operations } = setup();
  await saveProfileSettings({ avatar: null }, operations);
  assert.deepEqual(calls, []);
});

test("profile saving needs no password operation or validation", async () => {
  const { calls, operations } = setup();
  await saveProfileSettings({ username: "new", avatar: null }, operations);
  assert.deepEqual(calls, ["name", "readback", "refresh"]);
});

test("acknowledged avatar upload clears the pending file before readback", async () => {
  const { calls, operations } = setup();
  await saveProfileSettings({ username: "new", avatar: {} }, operations);
  assert.deepEqual(calls, [
    "name",
    "avatar",
    "clear-avatar",
    "readback",
    "refresh",
  ]);
});

test("failed readback accurately reports saved profile", async () => {
  const failure = new Error("network");
  const { calls, operations } = setup({
    readUser: async () => {
      throw failure;
    },
  });
  await assert.rejects(
    saveProfileSettings({ username: "new", avatar: null }, operations),
    (e) =>
      e instanceof AccountSettingsSaveError &&
      e.stage === "readback" &&
      e.profileSaved &&
      e.reason === failure,
  );
  assert.deepEqual(calls, ["name"]);
});

test("upload failure refreshes saved username and preserves the pending avatar", async () => {
  const failure = new Error("upload");
  const { calls, operations } = setup({
    uploadAvatar: async () => {
      throw failure;
    },
  });
  await assert.rejects(
    saveProfileSettings({ username: "new", avatar: {} }, operations),
    (e) => e.stage === "avatar" && e.profileSaved && e.reason === failure,
  );
  assert.deepEqual(calls, ["name", "readback", "refresh"]);
});

test("failed username write does not upload avatar or report success", async () => {
  const failure = new Error("write");
  const { calls, operations } = setup({
    updateUsername: async () => {
      throw failure;
    },
  });
  await assert.rejects(
    saveProfileSettings({ username: "new", avatar: {} }, operations),
    (e) => e.stage === "profile" && !e.profileSaved && e.reason === failure,
  );
  assert.deepEqual(calls, []);
});

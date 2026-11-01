type Changes<Avatar> = {
  username?: string;
  avatar: Avatar | null;
};

type Operations<Avatar, User> = {
  updateUsername: (username: string) => Promise<unknown>;
  uploadAvatar: (avatar: Avatar) => Promise<unknown>;
  avatarSaved: () => void;
  readUser: () => Promise<User>;
  onUser: (user: User) => void;
};

export class AccountSettingsSaveError extends Error {
  stage: "profile" | "avatar" | "readback";
  profileSaved: boolean;
  reason: unknown;

  constructor(
    stage: AccountSettingsSaveError["stage"],
    profileSaved: boolean,
    reason: unknown,
  ) {
    super("Account settings save failed");
    this.stage = stage;
    this.profileSaved = profileSaved;
    this.reason = reason;
  }
}

// 资料保存与联系方式变更独立提交。
export async function saveProfileSettings<Avatar, User>(
  changes: Changes<Avatar>,
  operations: Operations<Avatar, User>,
) {
  let stage: AccountSettingsSaveError["stage"] = "profile";
  let profileSaved = false;
  let refreshed = false;
  try {
    if (changes.username !== undefined) {
      await operations.updateUsername(changes.username);
      profileSaved = true;
    }
    if (changes.avatar !== null) {
      stage = "avatar";
      await operations.uploadAvatar(changes.avatar);
      profileSaved = true;
      operations.avatarSaved();
    }
    if (profileSaved) {
      stage = "readback";
      operations.onUser(await operations.readUser());
      refreshed = true;
    }
  } catch (reason) {
    if (profileSaved && !refreshed && stage !== "readback") {
      try {
        operations.onUser(await operations.readUser());
      } catch {
        // Preserve the original failure; the next open reads the session again.
      }
    }
    throw new AccountSettingsSaveError(stage, profileSaved, reason);
  }
}

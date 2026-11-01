-- 用户确认后的清空操作只保存范围、无正文的记录 ID 和执行进度，支持跨进程续清。
CREATE TABLE chat_history_deletions (
    user_id bigint NOT NULL DEFAULT NULLIF(current_setting('app.user_id',true),'')::bigint,
    id text NOT NULL,
    account_id bigint NOT NULL,
    conversation_id bigint NOT NULL,
    state text NOT NULL CHECK(state IN ('waiting','files','complete')),
    manifest jsonb NOT NULL DEFAULT '{}',
    owner text,
    lease_until double precision,
    next_at double precision NOT NULL DEFAULT 0,
    error text NOT NULL DEFAULT '',
    message_count bigint NOT NULL DEFAULT 0,
    created_at double precision NOT NULL,
    completed_at double precision,
    PRIMARY KEY(user_id,id),
    FOREIGN KEY(user_id,account_id) REFERENCES accounts(user_id,id),
    FOREIGN KEY(user_id,conversation_id) REFERENCES conversations(user_id,id)
);
CREATE UNIQUE INDEX chat_history_deletions_active ON chat_history_deletions(user_id,conversation_id) WHERE state<>'complete';
CREATE INDEX chat_history_deletions_pending ON chat_history_deletions(user_id,next_at) WHERE state<>'complete';
ALTER TABLE chat_history_deletions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_history_deletions FORCE ROW LEVEL SECURITY;
CREATE POLICY user_isolation ON chat_history_deletions
 USING(user_id=NULLIF(current_setting('app.user_id',true),'')::bigint)
 WITH CHECK(user_id=NULLIF(current_setting('app.user_id',true),'')::bigint);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wechat_manager_app') THEN
  GRANT SELECT,INSERT,UPDATE,DELETE ON chat_history_deletions TO wechat_manager_app;
 END IF;
END $$;
INSERT INTO schema_versions VALUES(59);

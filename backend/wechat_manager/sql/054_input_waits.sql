-- 明确等待仅持久化原文引用；不创建主动发送任务。
CREATE TABLE reply_input_waits (
    user_id bigint NOT NULL DEFAULT NULLIF(current_setting('app.user_id',true),'')::bigint,
    account_id bigint NOT NULL,
    conversation_id bigint NOT NULL,
    sender text NOT NULL,
    session_id text NOT NULL,
    owner_wxid text NOT NULL,
    app_id text NOT NULL,
    account_epoch bigint NOT NULL,
    conversation_epoch bigint NOT NULL,
    config_version bigint NOT NULL,
    source_mid bigint NOT NULL,
    started_at double precision NOT NULL,
    expires_at double precision NOT NULL,
    state text NOT NULL CHECK(state IN ('waiting','releasing','closed')),
    message_ids jsonb NOT NULL DEFAULT '[]',
    release_task_id text,
    release_revision bigint,
    control jsonb NOT NULL DEFAULT '{}',
    closed_reason text NOT NULL DEFAULT '',
    PRIMARY KEY(user_id,session_id),
    FOREIGN KEY(user_id,account_id) REFERENCES accounts(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,conversation_id) REFERENCES conversations(user_id,id) ON DELETE CASCADE,
    FOREIGN KEY(user_id,source_mid) REFERENCES message_records(user_id,id)
);
ALTER TABLE reply_input_waits ENABLE ROW LEVEL SECURITY;
ALTER TABLE reply_input_waits FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_scope ON reply_input_waits
    USING (user_id=NULLIF(current_setting('app.user_id',true),'')::bigint)
    WITH CHECK (user_id=NULLIF(current_setting('app.user_id',true),'')::bigint);
CREATE INDEX reply_input_waits_release ON reply_input_waits(user_id,release_task_id);
CREATE UNIQUE INDEX reply_input_waits_active
    ON reply_input_waits(user_id,account_id,conversation_id,sender) WHERE state<>'closed';
ALTER TABLE reply_decisions DROP CONSTRAINT IF EXISTS reply_decisions_action_check;
ALTER TABLE reply_decisions ADD CONSTRAINT reply_decisions_action_check
    CHECK(action IN ('reply','silent','wait'));
INSERT INTO schema_versions VALUES(54);
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='wechat_manager_app') THEN
  GRANT SELECT,INSERT,UPDATE,DELETE ON reply_input_waits TO wechat_manager_app;
 END IF;
END $$;

-- Explicit phase A candidate schema. No old-runtime automatic migrations.
CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source (
 source_id TEXT NOT NULL, source_version TEXT NOT NULL, source_kind TEXT NOT NULL,
 capabilities_json TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 evidence_json TEXT NOT NULL, registration_digest TEXT NOT NULL, created_at_utc TEXT NOT NULL,
 PRIMARY KEY(source_id,source_version)
);
CREATE TABLE own_account (
 account_key TEXT PRIMARY KEY, platform TEXT NOT NULL DEFAULT 'douyin', platform_uid TEXT NOT NULL,
 source_id TEXT NOT NULL, source_version TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 UNIQUE(platform,platform_uid), FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version)
);
CREATE TABLE contact (
 person_key TEXT PRIMARY KEY, account_key TEXT NOT NULL REFERENCES own_account(account_key),
 object_kind TEXT NOT NULL CHECK(object_kind IN ('person','group')),
 verified INTEGER NOT NULL CHECK(verified IN (0,1)), UNIQUE(account_key,person_key)
);
CREATE TABLE identity_alias (
 account_key TEXT NOT NULL, person_key TEXT NOT NULL, object_kind TEXT NOT NULL,
 platform TEXT NOT NULL DEFAULT 'douyin', identity_kind TEXT NOT NULL, identity_value TEXT NOT NULL,
 source_id TEXT NOT NULL, source_version TEXT NOT NULL, verified INTEGER NOT NULL CHECK(verified IN (0,1)),
 evidence_json TEXT NOT NULL, mapping_state TEXT NOT NULL DEFAULT 'separate',
 PRIMARY KEY(account_key,object_kind,platform,identity_kind,identity_value),
 FOREIGN KEY(account_key,person_key) REFERENCES contact(account_key,person_key),
 FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version)
);
CREATE TABLE conversation (
 conversation_key TEXT PRIMARY KEY, account_key TEXT NOT NULL, person_key TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('person','group')), platform_conversation_key TEXT NOT NULL,
 UNIQUE(account_key,kind,platform_conversation_key), UNIQUE(account_key,conversation_key),
 FOREIGN KEY(account_key,person_key) REFERENCES contact(account_key,person_key)
);
CREATE TABLE coverage_segment (
 coverage_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_version TEXT NOT NULL,
 account_key TEXT NOT NULL REFERENCES own_account(account_key), conversation_key TEXT,
 coverage_json TEXT NOT NULL, observed_at_utc TEXT NOT NULL,
 FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version)
);
CREATE TABLE message (
 message_key TEXT PRIMARY KEY, account_key TEXT NOT NULL, conversation_key TEXT NOT NULL,
 person_key TEXT NOT NULL, platform_message_key TEXT, direction TEXT NOT NULL CHECK(direction IN ('in','out','system','unknown')),
 kind TEXT NOT NULL, occurred_lower_utc TEXT, occurred_upper_utc TEXT, time_precision TEXT NOT NULL,
 success_state TEXT NOT NULL CHECK(success_state IN ('confirmed','failed','pending','unknown')),
 identity_state TEXT NOT NULL, active_body_revision INTEGER NOT NULL, quality_json TEXT NOT NULL,
 platform_correlation_key TEXT, actor_state TEXT NOT NULL DEFAULT 'actor_unknown', actor_key TEXT,
 created_at_utc TEXT NOT NULL, UNIQUE(account_key,conversation_key,platform_message_key),
 UNIQUE(account_key,message_key), FOREIGN KEY(account_key,conversation_key) REFERENCES conversation(account_key,conversation_key),
 FOREIGN KEY(account_key,person_key) REFERENCES contact(account_key,person_key),
 FOREIGN KEY(message_key,active_body_revision) REFERENCES message_revision(message_key,revision) DEFERRABLE INITIALLY DEFERRED,
 CHECK((occurred_lower_utc IS NULL AND occurred_upper_utc IS NULL) OR (occurred_lower_utc IS NOT NULL AND occurred_upper_utc IS NOT NULL AND occurred_lower_utc<=occurred_upper_utc))
);
CREATE TABLE message_revision (
 message_key TEXT NOT NULL REFERENCES message(message_key), revision INTEGER NOT NULL,
 body TEXT NOT NULL, body_sha256 TEXT NOT NULL, semantic_digest TEXT NOT NULL,
 observation_key TEXT NOT NULL REFERENCES message_observation(observation_key) DEFERRABLE INITIALLY DEFERRED, created_at_utc TEXT NOT NULL,
 PRIMARY KEY(message_key,revision)
);
CREATE TABLE message_observation (
 observation_key TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_version TEXT NOT NULL,
 observation_id TEXT NOT NULL, account_key TEXT NOT NULL, message_key TEXT REFERENCES message(message_key),
 device_key TEXT NOT NULL, observed_at_utc TEXT NOT NULL, semantic_digest TEXT NOT NULL,
 payload_json TEXT NOT NULL, quality_json TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(source_id,source_version,observation_id),
 FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version)
);
CREATE INDEX idx_msg_history ON message(account_key,conversation_key,message_key DESC);
CREATE INDEX idx_msg_person ON message(account_key,person_key,message_key DESC);
CREATE INDEX idx_msg_time ON message(account_key,conversation_key,occurred_lower_utc,message_key);
CREATE INDEX idx_observation_message ON message_observation(message_key,device_key);
CREATE TABLE manual_action (
 action_key TEXT PRIMARY KEY, action_id TEXT NOT NULL, account_key TEXT NOT NULL REFERENCES own_account(account_key),
 conversation_key TEXT NOT NULL, source_id TEXT NOT NULL, source_version TEXT NOT NULL,
 operator_key TEXT NOT NULL, device_key TEXT NOT NULL, session_id TEXT NOT NULL,
 action_kind TEXT NOT NULL, platform_correlation_key TEXT, trusted INTEGER NOT NULL CHECK(trusted IN (0,1)),
 semantic_digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_utc TEXT NOT NULL,
 UNIQUE(source_id,source_version,action_id),
 FOREIGN KEY(account_key,conversation_key) REFERENCES conversation(account_key,conversation_key),
 FOREIGN KEY(source_id,source_version) REFERENCES source(source_id,source_version)
);
CREATE TABLE send_link (
 message_key TEXT PRIMARY KEY REFERENCES message(message_key), action_key TEXT UNIQUE REFERENCES manual_action(action_key),
 link_state TEXT NOT NULL, operator_key TEXT, linked_at_utc TEXT NOT NULL
);
CREATE TABLE event_sequence (
 device_key TEXT NOT NULL, epoch TEXT NOT NULL, channel TEXT NOT NULL, seq INTEGER NOT NULL,
 PRIMARY KEY(device_key,epoch,channel)
);
CREATE TABLE domain_event (
 event_id TEXT PRIMARY KEY, device_key TEXT NOT NULL, epoch TEXT NOT NULL, channel TEXT NOT NULL,
 seq INTEGER NOT NULL, account_key TEXT NOT NULL, target_key TEXT NOT NULL, event_type TEXT NOT NULL,
 payload_hash TEXT NOT NULL, event_digest TEXT NOT NULL, parents_json TEXT NOT NULL, event_json TEXT NOT NULL,
 created_at_utc TEXT NOT NULL, UNIQUE(device_key,epoch,channel,seq)
);
CREATE TABLE outbox (
 event_id TEXT PRIMARY KEY REFERENCES domain_event(event_id), state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, created_at_utc TEXT NOT NULL
);
CREATE INDEX idx_outbox_pending ON outbox(state,next_attempt_at);
CREATE TABLE migration_journal (
 migration_key TEXT PRIMARY KEY, source_digest TEXT NOT NULL, migration_version TEXT NOT NULL,
 counts_json TEXT NOT NULL, state TEXT NOT NULL, created_at_utc TEXT NOT NULL
);
CREATE TABLE legacy_map (
 migration_key TEXT NOT NULL REFERENCES migration_journal(migration_key), old_table TEXT NOT NULL,
 old_record_key TEXT NOT NULL, new_key TEXT NOT NULL, mapping_state TEXT NOT NULL,
 PRIMARY KEY(migration_key,old_table,old_record_key)
);
CREATE TABLE legacy_record (
 legacy_key TEXT PRIMARY KEY, migration_key TEXT NOT NULL REFERENCES migration_journal(migration_key),
 old_table TEXT NOT NULL, old_record_key TEXT NOT NULL, original_json TEXT NOT NULL,
 original_sha256 TEXT NOT NULL, classification TEXT NOT NULL, reasons_json TEXT NOT NULL,
 UNIQUE(migration_key,old_table,old_record_key)
);
CREATE TABLE deletion_tombstone (
 target_key TEXT PRIMARY KEY, deletion_state TEXT NOT NULL DEFAULT 'copies_pending', created_at_utc TEXT NOT NULL
);
PRAGMA user_version=2;

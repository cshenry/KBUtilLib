-- The council store's SCHEMA VERSION 1 DDL, exactly as KBUtilLib main 16e3fe4
-- created it (dumped from sqlite_master after metadata.create_all on the M1
-- store/schema.py). Checked in rather than generated so the v1 -> v2 migration
-- test runs against a REAL v1 database and cannot drift when schema.py does.
--
-- The five tables version 2 drops are here: daemon_lease, local_models,
-- model_resources, model_loads, gpu_lease. endpoint_state is keyed on the
-- submitting user, which is what the migration re-keys onto "local".

CREATE TABLE attempts (
    	call_id VARCHAR(64) NOT NULL, 
    	attempt_no INTEGER NOT NULL, 
    	started_at DATETIME, 
    	finished_at DATETIME, 
    	outcome_kind VARCHAR(32), 
    	usage JSON, 
    	worker_id VARCHAR(64), 
    	PRIMARY KEY (call_id, attempt_no), 
    	FOREIGN KEY(call_id) REFERENCES calls (call_id)
    );

CREATE TABLE calls (
    	call_id VARCHAR(64) NOT NULL, 
    	job_id VARCHAR(64) NOT NULL, 
    	item_id VARCHAR(255) NOT NULL, 
    	role VARCHAR(32) NOT NULL, 
    	seat_name VARCHAR(255) NOT NULL, 
    	endpoint VARCHAR(255) NOT NULL, 
    	endpoint_kind VARCHAR(32) NOT NULL, 
    	served_model VARCHAR(255), 
    	canonical_model VARCHAR(255), 
    	route_fingerprint VARCHAR(64), 
    	query_name VARCHAR(255), 
    	query_version VARCHAR(64), 
    	entity_hash VARCHAR(64), 
    	data_hash VARCHAR(64), 
    	prompt_hash VARCHAR(64), 
    	upstream_digest VARCHAR(64) NOT NULL, 
    	status VARCHAR(32) NOT NULL, 
    	not_before DATETIME, 
    	claim_token VARCHAR(64), 
    	claimed_by VARCHAR(64), 
    	claimed_at DATETIME, 
    	lease_generation INTEGER, 
    	attempt_count INTEGER NOT NULL, 
    	reply_text TEXT, 
    	reply_parsed JSON, 
    	normalised JSON, 
    	fresh_usage JSON, 
    	error JSON, 
    	reused_from VARCHAR(64), 
    	submitted_by VARCHAR(255) NOT NULL, 
    	created_at DATETIME NOT NULL, 
    	finished_at DATETIME, 
    	live BOOLEAN NOT NULL, 
    	PRIMARY KEY (call_id), 
    	CONSTRAINT fk_calls_item FOREIGN KEY(job_id, item_id) REFERENCES items (job_id, item_id)
    );

CREATE TABLE daemon_lease (
    	user VARCHAR(255) NOT NULL, 
    	holder_worker_id VARCHAR(64), 
    	generation INTEGER NOT NULL, 
    	expires_at DATETIME, 
    	PRIMARY KEY (user)
    );

CREATE TABLE endpoint_state (
    	user VARCHAR(255) NOT NULL, 
    	endpoint VARCHAR(255) NOT NULL, 
    	budget_epoch INTEGER NOT NULL, 
    	epoch_tokens INTEGER NOT NULL, 
    	lifetime_tokens INTEGER NOT NULL, 
    	halt_kind VARCHAR(32), 
    	halt_message TEXT, 
    	updated_at DATETIME, 
    	PRIMARY KEY (user, endpoint)
    );

CREATE TABLE gpu_lease (
    	id INTEGER NOT NULL, 
    	holder_worker_id VARCHAR(64), 
    	generation INTEGER NOT NULL, 
    	expires_at DATETIME, 
    	PRIMARY KEY (id)
    );

CREATE TABLE items (
    	job_id VARCHAR(64) NOT NULL, 
    	item_id VARCHAR(255) NOT NULL, 
    	entity_hash VARCHAR(64), 
    	data_hash VARCHAR(64), 
    	evidence JSON, 
    	menu JSON, 
    	item_order INTEGER NOT NULL, 
    	status VARCHAR(32) NOT NULL, 
    	verdict JSON, 
    	decided_at DATETIME, 
    	PRIMARY KEY (job_id, item_id), 
    	FOREIGN KEY(job_id) REFERENCES jobs (job_id)
    );

CREATE TABLE jobs (
    	job_id VARCHAR(64) NOT NULL, 
    	label TEXT, 
    	deleted_at DATETIME, 
    	query_name VARCHAR(255) NOT NULL, 
    	query_version VARCHAR(64) NOT NULL, 
    	submitted_by VARCHAR(255) NOT NULL, 
    	submitted_host VARCHAR(255), 
    	submitted_at DATETIME NOT NULL, 
    	priority INTEGER NOT NULL, 
    	status VARCHAR(32) NOT NULL, 
    	roster JSON, 
    	roster_fingerprint VARCHAR(64), 
    	profile_name VARCHAR(255), 
    	options JSON, 
    	parent_job_id VARCHAR(64), 
    	n_items INTEGER NOT NULL, 
    	PRIMARY KEY (job_id)
    );

CREATE TABLE local_models (
    	alias VARCHAR(255) NOT NULL, 
    	served_name VARCHAR(255), 
    	canonical_model VARCHAR(255), 
    	endpoint_url TEXT, 
    	control_url TEXT, 
    	gpu_mem_gib FLOAT, 
    	gpu_count INTEGER, 
    	default_gpus JSON, 
    	registered_by VARCHAR(255), 
    	registered_at DATETIME, 
    	notes TEXT, 
    	PRIMARY KEY (alias)
    );

CREATE TABLE model_loads (
    	alias VARCHAR(255) NOT NULL, 
    	loaded_at DATETIME NOT NULL, 
    	gpus JSON, 
    	loaded_by_user VARCHAR(255), 
    	by_council BOOLEAN NOT NULL, 
    	instance_started_at DATETIME, 
    	instance_pid INTEGER, 
    	last_used_at DATETIME, 
    	dropped_at DATETIME, 
    	PRIMARY KEY (alias, loaded_at)
    );

CREATE TABLE model_resources (
    	alias VARCHAR(255) NOT NULL, 
    	min_gpu_count INTEGER, 
    	per_gpu_peak_gib FLOAT, 
    	startup_settings JSON, 
    	measured_by VARCHAR(255), 
    	measured_at DATETIME, 
    	notes TEXT, 
    	PRIMARY KEY (alias)
    );

CREATE TABLE query_types (
    	name VARCHAR(255) NOT NULL, 
    	version VARCHAR(64) NOT NULL, 
    	status VARCHAR(32) NOT NULL, 
    	spec JSON NOT NULL, 
    	source VARCHAR(32) NOT NULL, 
    	created_by VARCHAR(255), 
    	created_at DATETIME, 
    	PRIMARY KEY (name, version)
    );

CREATE TABLE schema_version (
    	version INTEGER NOT NULL, 
    	PRIMARY KEY (version)
    );

CREATE TABLE workers (
    	worker_id VARCHAR(64) NOT NULL, 
    	user VARCHAR(255) NOT NULL, 
    	kind VARCHAR(32) NOT NULL, 
    	host VARCHAR(255), 
    	pid INTEGER, 
    	started_at DATETIME, 
    	heartbeat_at DATETIME, 
    	version VARCHAR(64), 
    	endpoints JSON, 
    	stop_requested_at DATETIME, 
    	PRIMARY KEY (worker_id)
    );

CREATE INDEX ix_calls_job_item ON calls (job_id, item_id);

CREATE INDEX ix_calls_prompt_hash_status ON calls (prompt_hash, status);

CREATE INDEX ix_calls_query_model_entity ON calls (query_name, canonical_model, entity_hash);

CREATE INDEX ix_calls_status_not_before ON calls (status, not_before);

CREATE INDEX ix_items_job_status ON items (job_id, status);

CREATE INDEX ix_items_order ON items (job_id, item_order);

CREATE INDEX ix_jobs_priority ON jobs (priority, submitted_at);

CREATE INDEX ix_jobs_user_status ON jobs (submitted_by, status);

CREATE INDEX ix_workers_user_heartbeat ON workers (user, heartbeat_at);

CREATE UNIQUE INDEX ux_calls_one_live ON calls (job_id, item_id, role, seat_name, upstream_digest) WHERE live;

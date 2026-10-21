"""Migration 002 (§17.3.4) — schema version 2."""
MIGRATION_002 = [
    "CREATE TABLE IF NOT EXISTS eval_sets(set_id TEXT PRIMARY KEY, role TEXT NOT NULL, content_hash TEXT NOT NULL, n_instances INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS eval_set_usage(set_id TEXT NOT NULL, campaign_id TEXT NOT NULL, candidate_id TEXT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS candidates(candidate_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, round INTEGER NOT NULL, parent_commit TEXT NOT NULL, commit_id TEXT, tier TEXT NOT NULL, status TEXT NOT NULL, children INTEGER NOT NULL DEFAULT 0, gate_decision_ref TEXT, observations_ref TEXT, edit_text_ref TEXT)",
    "CREATE TABLE IF NOT EXISTS promotions(promotion_id TEXT PRIMARY KEY, campaign_id TEXT, candidate_id TEXT, repo TEXT NOT NULL, commit_id TEXT NOT NULL, tier TEXT NOT NULL, approvals TEXT, promoted_at TEXT NOT NULL, rolled_back_at TEXT)",
    "CREATE TABLE IF NOT EXISTS negative_results(id TEXT PRIMARY KEY, campaign_id TEXT, pattern_id TEXT, edit_summary_ref TEXT NOT NULL, reason TEXT NOT NULL, stats_ref TEXT, created_at TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS negative_results_fts USING fts5(text, id UNINDEXED)",
    "CREATE TABLE IF NOT EXISTS observation_cache(key TEXT PRIMARY KEY, config_commit TEXT NOT NULL, set_id TEXT NOT NULL, model_ref TEXT NOT NULL, seeds_hash TEXT NOT NULL, observations_ref TEXT NOT NULL, created_at TEXT NOT NULL)",
]


async def apply_migration_002(writer) -> None:
    for stmt in MIGRATION_002:
        await writer.execute(stmt)

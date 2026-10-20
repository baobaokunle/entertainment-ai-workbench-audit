"""Explicit add-on migrations. Normal startup and GET never install or rebuild."""
from .common import utc_now
def install_candidate_features(db):
    from .sync.schema import install_schema as sync
    from .projections import install_schema as projections
    from .history_index import install_schema as history
    from .activity import install_schema as activity
    from .activity_adapter import install_schema as activity_adapter
    from .activity_retention import install_schema as activity_retention
    from .ai_annotations import install_schema as ai
    with db.transaction() as conn:
        sync(db);projections(db);history(db);activity(db);activity_adapter(db);activity_retention(db);ai(db)
        conn.execute("INSERT INTO app_meta VALUES('candidate_ai','1') ON CONFLICT(key) DO NOTHING")
        conn.execute("INSERT INTO app_meta VALUES('candidate_features','1') ON CONFLICT(key) DO NOTHING")
        conn.execute("INSERT INTO app_meta VALUES('candidate_migrated_at',?) ON CONFLICT(key) DO NOTHING",(utc_now(),))

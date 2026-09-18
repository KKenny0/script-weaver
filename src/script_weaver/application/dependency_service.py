"""Explicit v1 dependency propagation through reference bindings."""

import json
from datetime import UTC, datetime
from uuid import uuid4


class DependencyService:
    @staticmethod
    def mark_follow_latest_stale(
        connection, asset_id: str, version_id: str, exclude_media_id: str | None = None
    ) -> int:
        reason = f"asset version {version_id} is current"
        shot_ids = [row[0] for row in connection.execute(
            """SELECT DISTINCT shot_id FROM reference_bindings
            WHERE asset_id=? AND binding_mode='follow_latest' AND asset_version_id<>?""",
            (asset_id, version_id),
        )]
        cursor = connection.execute(
            """UPDATE reference_bindings SET is_stale=1, stale_reason=?
               WHERE asset_id=? AND binding_mode='follow_latest' AND asset_version_id<>?""",
            (reason, asset_id, version_id),
        )
        if shot_ids:
            placeholders = ",".join("?" for _ in shot_ids)
            connection.execute(
                f"""UPDATE shots SET is_stale=1,stale_reason=?,revision=revision+1
                WHERE id IN ({placeholders})""",
                (reason, *shot_ids),
            )
            for shot_id in shot_ids:
                row = dict(connection.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone())
                connection.execute(
                    "INSERT INTO shot_versions VALUES(?,?,?,?,NULL,?)",
                    (str(uuid4()), shot_id, row["revision"],
                     json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                     datetime.now(UTC).isoformat()),
                )
            connection.execute(
                f"""UPDATE prompt_versions SET is_stale=1,stale_reason=?
                WHERE owner_type='shot' AND owner_id IN ({placeholders})""",
                (reason, *shot_ids),
            )
            media_exclusion = " AND id<>?" if exclude_media_id else ""
            connection.execute(
                f"""UPDATE media_versions SET is_stale=1,stale_reason=?
                WHERE owner_type='shot' AND owner_id IN ({placeholders}){media_exclusion}""",
                (reason, *shot_ids, *([exclude_media_id] if exclude_media_id else [])),
            )
        return cursor.rowcount

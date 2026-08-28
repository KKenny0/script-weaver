"""Explicit v1 dependency propagation through reference bindings."""


class DependencyService:
    @staticmethod
    def mark_follow_latest_stale(connection, asset_id: str, version_id: str) -> int:
        cursor = connection.execute(
            """UPDATE reference_bindings SET is_stale=1, stale_reason=?
               WHERE asset_id=? AND binding_mode='follow_latest' AND asset_version_id<>?""",
            (f"asset version {version_id} is current", asset_id, version_id),
        )
        return cursor.rowcount

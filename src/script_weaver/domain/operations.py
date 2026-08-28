"""Operation names allowed across the agent trust boundary."""

ALLOWED_OPERATIONS = {
    "segment.create", "shot.create", "shot.update", "shot.retire", "shot.reorder",
    "asset.create", "asset.version.create", "reference.bind", "reference.rebind",
    "reference.set_mode", "prompt.version.create",
}

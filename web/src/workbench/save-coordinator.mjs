export function createSaveCoordinator({ read, write, settled, failed, busy }) {
  let queue = Promise.resolve();
  let pending = 0;

  const save = () => {
    pending += 1;
    busy(true);
    const run = async () => {
      const snapshot = { ...read() };
      if (!snapshot.dirty) return snapshot.revision;
      try {
        const revision = await write(snapshot);
        const current = read();
        settled(snapshot, revision, current.documentId === snapshot.documentId && current.content === snapshot.content);
        return revision;
      } catch (error) {
        failed(snapshot, error);
        throw error;
      }
    };
    const result = queue.then(run, run);
    queue = result.catch(() => undefined);
    return result.finally(() => {
      pending -= 1;
      busy(pending > 0);
    });
  };

  const flush = async (documentId) => {
    for (;;) {
      const revision = await save();
      const current = read();
      if (current.documentId !== documentId) throw new Error("document changed while saving");
      if (!current.dirty) return revision;
    }
  };

  return { save, flush };
}

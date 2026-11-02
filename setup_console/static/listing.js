// Which stacks the Infrastructure and JFrog OIDC pages list, and the counts above them: one rule for
// both, no DOM, so tests run it (setup_console/tests/test_listing.py).

// Gone outside the console while its state still lists resources: residue to destroy.
export const partlyDeleted = (s) => Boolean(s.status?.deleted && Object.keys(s.status.outputs || {}).length);
export const lastFailure = (s) => (s.failed && !s.busy && !(s.failed.action === "status" && s.needs?.length) ? s.failed : null);
export const stateOf = (s) => (s.status?.deployed ? "deployed"
  : partlyDeleted(s) || lastFailure(s) || s.missing?.length ? "attention" : "not");
// Listed on the live page: deployed, under way, or needing attention (residue or a failed run).
export const standing = (s) => Boolean(s.status?.deployed || s.busy || s.plan || s.job || stateOf(s) === "attention");

// The Essentials counts, over exactly the stacks the live page lists.
export function counts(stacks) {
  const live = stacks.filter(standing);
  return {
    listed: live.length,
    deployed: live.filter((s) => s.status?.deployed).length,
    attention: live.filter((s) => stateOf(s) === "attention").length,
    busy: live.filter((s) => s.busy || s.plan).length,
    archived: stacks.length - live.length,
  };
}

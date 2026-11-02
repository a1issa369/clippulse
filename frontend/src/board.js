// A shotlist "board" is identified by a random 32 character hex id. Whoever has the id can
// view and edit it, which is the whole sharing model (no accounts). The id lives in
// localStorage and can travel in a ?board= link.

export const BOARD_KEY = "clippulse.board";
const BOARD_RE = /^[0-9a-f]{32}$/;

export function isBoardId(value) {
  return typeof value === "string" && BOARD_RE.test(value);
}

export function randomBoardId(cryptoObj = globalThis.crypto) {
  const bytes = new Uint8Array(16);
  cryptoObj.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function safeGet(storage, key) {
  try { return storage?.getItem(key) ?? null; } catch { return null; }
}

function safeSet(storage, key, value) {
  try { storage?.setItem(key, value); } catch { /* private mode or blocked storage: the board just won't persist */ }
}

/** Returns { id, fromLink }. A valid ?board= link wins, then the saved id, then a fresh one. */
export function resolveBoardId({ search = "", storage, random = randomBoardId } = {}) {
  const linked = new URLSearchParams(search).get("board");
  if (isBoardId(linked)) {
    safeSet(storage, BOARD_KEY, linked);
    return { id: linked, fromLink: true };
  }
  const saved = safeGet(storage, BOARD_KEY);
  if (isBoardId(saved)) return { id: saved, fromLink: false };
  const id = random();
  safeSet(storage, BOARD_KEY, id);
  return { id, fromLink: false };
}

export function shareUrl(origin, boardId) {
  return `${origin}/?board=${boardId}`;
}

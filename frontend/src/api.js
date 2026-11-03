// Thin client for the ClipPulse API. Same origin: CloudFront (or the dev server) routes /api/*.

export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export function createApi(fetchFn = (...args) => globalThis.fetch(...args), base = "/api") {
  async function request(path, { method = "GET", body, signal } = {}) {
    let response;
    try {
      response = await fetchFn(base + path, {
        method,
        signal,
        headers: body === undefined ? undefined : { "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (err) {
      if (err?.name === "AbortError") throw err;
      throw new ApiError("Could not reach ClipPulse. Check your connection and try again.", 0, "network");
    }
    if (response.status === 204) return null;
    let data = null;
    try { data = await response.json(); } catch { /* non-JSON error page */ }
    if (!response.ok) {
      // API Gateway's own throttle answers 429 with a plain body, so give that case a friendly message too.
      const fallback = response.status === 429
        ? "ClipPulse is busy right now. Try again in a moment."
        : `Request failed (${response.status}).`;
      throw new ApiError(data?.error?.message ?? fallback, response.status, data?.error?.code);
    }
    return data;
  }

  const query = (params) => {
    const text = new URLSearchParams(params).toString();
    return text ? `?${text}` : "";
  };

  return {
    listStreamers: (signal) => request("/streamers", { signal }),
    trackStreamer: (login) => request("/streamers", { method: "POST", body: { login } }),
    removeStreamer: (login) => request(`/streamers/${encodeURIComponent(login)}`, { method: "DELETE" }),
    getClips: (login, { sort, days }, signal) =>
      request(`/streamers/${encodeURIComponent(login)}/clips${query({ sort, days })}`, { signal }),
    getRising: ({ hours }, signal) => request(`/rising${query({ hours, limit: 24 })}`, { signal }),
    getBoard: (boardId, signal) => request(`/boards/${boardId}`, { signal }),
    saveItem: (boardId, clipId, { streamer, note }) =>
      request(`/boards/${boardId}/items/${encodeURIComponent(clipId)}`, { method: "PUT", body: { streamer, note } }),
    removeItem: (boardId, clipId) =>
      request(`/boards/${boardId}/items/${encodeURIComponent(clipId)}`, { method: "DELETE" }),
    exportUrl: (boardId) => `${base}/boards/${boardId}/export.csv`,
  };
}

export const api = createApi();

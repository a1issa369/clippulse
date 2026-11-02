import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

const REFRESH_MS = 60_000;

/** Loads the clip feed for the current view and refreshes it once a minute while the tab is visible. */
export function useFeed({ view, sort, days, hours }) {
  const key = `${view}|${sort}|${days}|${hours}`;
  const [state, setState] = useState({ key: null, status: "loading", clips: [], asOf: null, error: null });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    const request = view === "rising"
      ? api.getRising({ hours }, controller.signal)
      : api.getClips(view, { sort, days }, controller.signal);
    request
      .then((data) => setState({ key, status: "ready", clips: data.clips, asOf: data.as_of, error: null }))
      .catch((err) => {
        if (err?.name === "AbortError") return;
        // Keep showing what we had for this same view if only a background refresh failed.
        setState((prev) => ({ key, status: "error", clips: prev.key === key ? prev.clips : [], asOf: null, error: err.message }));
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);

  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") setTick((t) => t + 1);
    }, REFRESH_MS);
    return () => clearInterval(id);
  }, []);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, loading: state.key !== key, reload };
}

export function useStreamers() {
  const [streamers, setStreamers] = useState([]);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.listStreamers();
      setStreamers(data.streamers);
      setStatus("ready");
      setError(null);
    } catch (err) {
      setStatus("error");
      setError(err.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  /** Returns the tracked streamer's login, or throws an ApiError with a message fit to show. */
  const track = useCallback(async (login) => {
    const result = await api.trackStreamer(login);
    await refresh();
    return result.streamer.login;
  }, [refresh]);

  return { streamers, status, error, track };
}

export function useBoard(boardId) {
  const [items, setItems] = useState([]);
  const [status, setStatus] = useState("loading");
  const [error, setError] = useState(null);

  const load = useCallback(async (signal) => {
    try {
      const data = await api.getBoard(boardId, signal);
      setItems(data.items);
      setStatus("ready");
    } catch (err) {
      if (err?.name === "AbortError") return;
      setStatus("error");
      setError(err.message);
    }
  }, [boardId]);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const add = useCallback(async (clip) => {
    setError(null);
    try {
      const { item } = await api.saveItem(boardId, clip.id, { streamer: clip.streamer });
      setItems((prev) => (prev.some((i) => i.clip_id === item.clip_id) ? prev : [...prev, item]));
    } catch (err) {
      setError(err.message);
    }
  }, [boardId]);

  const remove = useCallback(async (clipId) => {
    setError(null);
    setItems((prev) => prev.filter((i) => i.clip_id !== clipId));
    try {
      await api.removeItem(boardId, clipId);
    } catch (err) {
      setError(err.message);
      load();
    }
  }, [boardId, load]);

  const saveNote = useCallback(async (clipId, note) => {
    setError(null);
    try {
      const { item } = await api.saveItem(boardId, clipId, { note });
      setItems((prev) => prev.map((i) => (i.clip_id === clipId ? item : i)));
    } catch (err) {
      setError(err.message);
    }
  }, [boardId]);

  return { items, status, error, add, remove, saveNote, clearError: () => setError(null) };
}

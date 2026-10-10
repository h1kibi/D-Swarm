"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "./useRun";

export interface Event {
  id: string;
  name: string;
  description: string;
  status: "draft" | "active" | "archived";
  created_by: string;
  created_at: number;
  target_count: number;
}

export interface EventsState {
  events: Event[];
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

export function useEvents(): EventsState {
  const [events, setEvents] = useState<Event[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const res = await apiFetch("/api/events");
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        setEvents([]);
        return;
      }

      const body = (await res.json()) as unknown;
      const eventsList =
        body && typeof body === "object" && "events" in body
          ? ((body as Record<string, unknown>).events as Event[])
          : [];
      setEvents(eventsList);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load events");
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { events, loading, error, refresh };
}

export async function createEvent(data: {
  name: string;
  description: string;
}): Promise<{ ok: boolean; event?: Event; error?: string }> {
  try {
    const res = await apiFetch("/api/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to create event";
      return { ok: false, error: detail };
    }

    const result = (await res.json()) as unknown;
    const event =
      result && typeof result === "object" && "event" in result
        ? ((result as Record<string, unknown>).event as Event)
        : undefined;

    return { ok: true, event };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function updateEvent(
  id: string,
  data: Partial<{ name: string; description: string; status: string }>
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/events/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to update event";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

export async function deleteEvent(id: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await apiFetch(`/api/events/${id}`, { method: "DELETE" });

    if (!res.ok) {
      const body = (await res.json()) as unknown;
      const detail =
        body && typeof body === "object" && "detail" in body
          ? String((body as Record<string, unknown>).detail)
          : "Failed to delete event";
      return { ok: false, error: detail };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "An error occurred" };
  }
}

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ??
  "http://127.0.0.1:8000";
const DEFAULT_TIMEOUT_MS = 240_000;

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!response.ok) {
    if (text) {
      try {
        const payload = JSON.parse(text) as { detail?: unknown };
        const detail = payload.detail;
        if (typeof detail === "string") throw new Error(detail);
        if (detail && typeof detail === "object" && "message" in detail) {
          throw new Error(String((detail as { message: unknown }).message));
        }
        throw new Error(JSON.stringify(detail ?? payload));
      } catch (error) {
        if (error instanceof Error && error.message !== "Unexpected end of JSON input") {
          throw error;
        }
      }
    }
    throw new Error(text || `Request failed with ${response.status}`);
  }
  return text ? (JSON.parse(text) as T) : ({} as T);
}

async function fetchWithTimeout(
  url: string,
  options: RequestInit,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    cache: "no-store",
  });
  return readJson<T>(response);
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }, timeoutMs);
  return readJson<T>(response);
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readJson<T>(response);
}

export async function apiUpload<T>(path: string, body: FormData): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "POST",
    body,
  });
  return readJson<T>(response);
}

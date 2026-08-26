const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "")
  .trim()
  .replace(/\/$/, "");
const DEFAULT_TIMEOUT_MS = 240_000;

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function apiFileUrl(path: string): string {
  return `${API_BASE}${path}`;
}

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!response.ok) {
    if (
      response.status === 401 &&
      typeof window !== "undefined" &&
      window.location.pathname !== "/login"
    ) {
      const next = `${window.location.pathname}${window.location.search}`;
      window.location.assign(`/login?next=${encodeURIComponent(next)}`);
    }
    let detail: unknown = text;
    if (text) {
      try {
        const payload = JSON.parse(text) as { detail?: unknown };
        detail = payload.detail ?? payload;
      } catch {
        detail = text;
      }
    }
    const message =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail
          ? String((detail as { message: unknown }).message)
          : detail
            ? JSON.stringify(detail)
            : `Request failed with ${response.status}`;
    throw new ApiError(response.status, message, detail);
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
    return await fetch(url, {
      credentials: "include",
      ...options,
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function apiGet<T>(
  path: string,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    cache: "no-store",
  }, timeoutMs);
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

export async function apiPut<T>(
  path: string,
  body: unknown,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);
  return readJson<T>(response);
}

export async function apiPatch<T>(
  path: string,
  body: unknown,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);
  return readJson<T>(response);
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    method: "DELETE",
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

export async function apiUploadWithProgress<T>(
  path: string,
  body: FormData,
  onProgress: (percent: number) => void,
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE}${path}`);
    request.withCredentials = true;
    request.timeout = timeoutMs;
    request.upload.onprogress = (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
    };
    request.onerror = () => reject(new Error("Network request failed."));
    request.onabort = () => reject(new Error("Upload was cancelled."));
    request.ontimeout = () => reject(
      new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`),
    );
    request.onload = () => {
      const text = request.responseText;
      if (request.status >= 200 && request.status < 300) {
        try {
          const payload = text ? (JSON.parse(text) as T) : ({} as T);
          onProgress(100);
          resolve(payload);
        } catch {
          reject(new Error("Server returned an invalid JSON response."));
        }
        return;
      }
      if (
        request.status === 401 &&
        typeof window !== "undefined" &&
        window.location.pathname !== "/login"
      ) {
        const next = `${window.location.pathname}${window.location.search}`;
        window.location.assign(`/login?next=${encodeURIComponent(next)}`);
      }
      let detail: unknown = text;
      if (text) {
        try {
          const payload = JSON.parse(text) as { detail?: unknown };
          detail = payload.detail ?? payload;
        } catch {
          detail = text;
        }
      }
      const message =
        typeof detail === "string"
          ? detail
          : detail && typeof detail === "object" && "message" in detail
            ? String((detail as { message: unknown }).message)
            : detail
              ? JSON.stringify(detail)
              : `Request failed with ${request.status}`;
      reject(new ApiError(request.status, message, detail));
    };
    request.send(body);
  });
}

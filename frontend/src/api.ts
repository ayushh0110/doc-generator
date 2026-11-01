import type {
  Artifact,
  ChatSession,
  Citation,
  Health,
  Message,
  Preview,
  Profile,
  Source,
  StoredFile,
  StreamEvent,
  VersionDetail,
  VersionSummary,
} from "./types";

const TOKEN_KEY = "redline.token";
const EMAIL_KEY = "redline.email";

function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export const session = {
  token: () => readStorage(TOKEN_KEY),
  email: () => readStorage(EMAIL_KEY),
  save(token: string, email: string) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(EMAIL_KEY, email);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(EMAIL_KEY);
  },
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

let onSignedOut = () => {};
export function whenSignedOut(handler: () => void) {
  onSignedOut = handler;
}

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  const token = session.token();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`/api${path}`, { ...init, headers });
  if (response.ok) return response;

  if (response.status === 401 && token) {
    session.clear();
    onSignedOut();
  }
  let message = `The server said ${response.status}`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail)) message = body.detail[0]?.msg ?? message;
  } catch {
    // Not JSON. The status message will do.
  }
  throw new ApiError(message, response.status);
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return (await send(path, init)).json();
}

function post<T>(path: string, body: unknown): Promise<T> {
  return json<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export const api = {
  health: () => json<Health>("/health"),
  register: (email: string, password: string) =>
    post<{ token: string; email: string }>("/auth/register", { email, password }),
  login: (email: string, password: string) =>
    post<{ token: string; email: string }>("/auth/login", { email, password }),

  sessions: () => json<ChatSession[]>("/sessions"),
  createSession: () => post<ChatSession>("/sessions", {}),
  deleteSession: (id: string) => send(`/sessions/${id}`, { method: "DELETE" }),
  messages: (id: string) => json<Message[]>(`/sessions/${id}/messages`),
  sources: (id: string) => json<Source[]>(`/sessions/${id}/sources`),

  files: (id: string) => json<StoredFile[]>(`/sessions/${id}/files`),
  upload(id: string, file: File) {
    const form = new FormData();
    form.append("upload", file);
    return json<StoredFile>(`/sessions/${id}/files`, { method: "POST", body: form });
  },
  fileAnalysis: (id: string) => json<Profile>(`/files/${id}/analysis`),

  artifacts: (id: string) => json<Artifact[]>(`/sessions/${id}/artifacts`),
  version: (id: string) => json<VersionDetail>(`/versions/${id}`),
  citations: (id: string) => json<Citation[]>(`/versions/${id}/citations`),
  preview: (id: string) => json<Preview>(`/versions/${id}/preview`),
  structure: (id: string) => json<Profile>(`/versions/${id}/structure`),
  revert: (artifactId: string, versionId: string) =>
    post<VersionSummary>(`/artifacts/${artifactId}/revert`, { version_id: versionId }),

  async previewImage(versionId: string, page: number): Promise<string> {
    const response = await send(`/versions/${versionId}/preview/${page}`);
    return URL.createObjectURL(await response.blob());
  },

  async download(path: string) {
    const response = await send(path);
    const header = response.headers.get("Content-Disposition") ?? "";
    const utf8 = /filename\*=UTF-8''([^;]+)/i.exec(header);
    const plain = /filename="?([^";]+)"?/i.exec(header);
    const name = utf8 ? decodeURIComponent(utf8[1]) : (plain?.[1] ?? "download");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);
  },

  async chat(id: string, message: string, fileIds: string[], onEvent: (event: StreamEvent) => void) {
    const response = await send(`/sessions/${id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, file_ids: fileIds }),
    });
    const reader = response.body!.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += value;
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (frame.startsWith("data: ")) onEvent(JSON.parse(frame.slice(6)));
        boundary = buffer.indexOf("\n\n");
      }
    }
  },
};

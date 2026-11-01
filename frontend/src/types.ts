export type ChatSession = { id: string; title: string; created_at: string; updated_at: string };

export type TraceStep = { agent: string; detail: string };

export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  file_ids: string[];
  artifact_version_id: string | null;
  trace: TraceStep[];
  created_at: string;
  pending?: boolean;
  failed?: boolean;
};

export type StoredFile = {
  id: string;
  filename: string;
  kind: string;
  size_bytes: number;
  warnings: string[];
  index_status: string;
  index_error: string | null;
  has_profile: boolean;
  created_at: string;
};

export type VersionSummary = {
  id: string;
  number: number;
  summary: string;
  instruction: string;
  created_at: string;
};

export type Artifact = {
  id: string;
  title: string;
  kind: string;
  source_file_id: string | null;
  created_at: string;
  versions: VersionSummary[];
};

export type CheckResult = { name: string; ok: boolean; detail: string; severity: string };

export type Change = {
  element: string;
  kind: "added" | "removed" | "changed";
  before: string | null;
  after: string | null;
};

export type VersionDetail = VersionSummary & {
  checks: CheckResult[];
  changes: Change[];
  operations: Record<string, unknown>[];
};

export type Source = {
  id: string;
  key: string;
  kind: "file" | "web";
  title: string;
  url: string | null;
  file_id: string | null;
  locator: string;
  snippet: string;
  retrieved_at: string;
};

export type Citation = { element: string; excerpt: string; source: Source };

export type Preview = { pages: number; available: boolean; reason: string | null };

export type Health = {
  status: string;
  llm: { provider: string; model: string; configured: boolean };
  search: string;
  web: string;
  previews: boolean;
};

export type StreamEvent =
  | { type: "step"; agent: string; detail: string }
  | { type: "token"; text: string }
  | { type: "artifact"; artifact_id: string; version_id: string; number: number; title: string; kind: string }
  | { type: "error"; message: string }
  | { type: "done"; message_id: string; failed: boolean };

export type Profile = Record<string, any>;

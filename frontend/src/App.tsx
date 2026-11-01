import { useCallback, useEffect, useRef, useState } from "react";

import { api, session, whenSignedOut } from "./api";
import Composer from "./components/Composer";
import Desk, { type DeskFocus } from "./components/Desk";
import Login from "./components/Login";
import Sidebar from "./components/Sidebar";
import Transcript from "./components/Transcript";
import type { Artifact, ChatSession, Health, Message, Source, StoredFile, TraceStep } from "./types";

export default function App() {
  const [email, setEmail] = useState<string | null>(session.token() ? session.email() : null);

  useEffect(() => whenSignedOut(() => setEmail(null)), []);

  if (!email) return <Login onDone={setEmail} />;
  return (
    <Workspace
      email={email}
      onSignOut={() => {
        session.clear();
        setEmail(null);
      }}
    />
  );
}

function Workspace({ email, onSignOut }: { email: string; onSignOut: () => void }) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [files, setFiles] = useState<StoredFile[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [focus, setFocus] = useState<DeskFocus>(null);
  const [highlightSource, setHighlightSource] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [railOpen, setRailOpen] = useState(false);
  const [composerKey, setComposerKey] = useState(0);
  const loadToken = useRef(0);
  const activeRef = useRef<string | null>(null);
  const creating = useRef<Promise<string> | null>(null);
  const skipLoadFor = useRef<string | null>(null);
  activeRef.current = activeId;

  useEffect(() => {
    api.sessions().then((rows) => {
      setSessions(rows);
      setActiveId((current) => current ?? rows[0]?.id ?? null);
    }, showError);
    api.health().then(setHealth, () => setHealth(null));
  }, []);

  function showError(reason: unknown) {
    setError(reason instanceof Error ? reason.message : "Something went wrong");
  }

  const refreshWorkspace = useCallback(async (id: string) => {
    const [nextFiles, nextArtifacts, nextSources] = await Promise.all([
      api.files(id),
      api.artifacts(id),
      api.sources(id),
    ]);
    setFiles(nextFiles);
    setArtifacts(nextArtifacts);
    setSources(nextSources);
    return nextArtifacts;
  }, []);

  useEffect(() => {
    if (activeId && activeId === skipLoadFor.current) {
      skipLoadFor.current = null;
      return;
    }
    const token = ++loadToken.current;
    setError(null);
    setFocus(null);
    if (!activeId) {
      setMessages([]);
      setFiles([]);
      setArtifacts([]);
      setSources([]);
      return;
    }
    Promise.all([api.messages(activeId), refreshWorkspace(activeId)])
      .then(([rows, nextArtifacts]) => {
        if (token !== loadToken.current) return;
        setMessages(rows);
        const latest = nextArtifacts.at(-1);
        if (latest) setFocus({ type: "artifact", artifactId: latest.id, versionId: latest.versions.at(-1)!.id });
      })
      .catch(showError);
  }, [activeId, refreshWorkspace]);

  async function newSession() {
    try {
      const created = await api.createSession();
      setSessions((rows) => [created, ...rows]);
      pickSession(created.id);
      setRailOpen(false);
    } catch (reason) {
      showError(reason);
    }
  }

  async function removeSession(id: string) {
    try {
      await api.deleteSession(id);
      setSessions((rows) => rows.filter((row) => row.id !== id));
      if (activeId === id) pickSession(sessions.find((row) => row.id !== id)?.id ?? null);
    } catch (reason) {
      showError(reason);
    }
  }

  // Several files can be attached before a session exists. They must all land in the same one.
  function ensureSession(): Promise<string> {
    if (activeRef.current) return Promise.resolve(activeRef.current);
    if (creating.current) return creating.current;
    creating.current = api
      .createSession()
      .then((created) => {
        setSessions((rows) => [created, ...rows]);
        // It's empty, and reloading could overwrite an upload that's already finished.
        skipLoadFor.current = created.id;
        activeRef.current = created.id;
        setActiveId(created.id);
        return created.id;
      })
      .finally(() => {
        creating.current = null;
      });
    return creating.current;
  }

  function pickSession(id: string | null) {
    setActiveId(id);
    // Attachments belong to the session they were uploaded to.
    setComposerKey((key) => key + 1);
  }

  async function upload(file: File): Promise<StoredFile> {
    const id = await ensureSession();
    const stored = await api.upload(id, file);
    setFiles((rows) => [...rows, stored]);
    if (stored.has_profile) setFocus({ type: "file", fileId: stored.id });
    return stored;
  }

  async function send(text: string, fileIds: string[]) {
    setError(null);
    setBusy(true);
    const id = await ensureSession().catch((reason) => {
      showError(reason);
      setBusy(false);
      return null;
    });
    if (!id) return;

    const now = new Date().toISOString();
    const reply: Message = {
      id: `pending-${now}`,
      role: "assistant",
      content: "",
      file_ids: [],
      artifact_version_id: null,
      trace: [],
      created_at: now,
      pending: true,
    };
    setMessages((rows) => [
      ...rows,
      { ...reply, id: `user-${now}`, role: "user", content: text, file_ids: fileIds, pending: false },
      reply,
    ]);
    const patchReply = (patch: (current: Message) => Message) =>
      setMessages((rows) => rows.map((row) => (row.id === reply.id ? patch(row) : row)));

    try {
      await api.chat(id, text, fileIds, (event) => {
        if (event.type === "step") {
          const step: TraceStep = { agent: event.agent, detail: event.detail };
          patchReply((row) => ({ ...row, trace: [...row.trace, step] }));
        } else if (event.type === "token") {
          patchReply((row) => ({ ...row, content: row.content + event.text }));
        } else if (event.type === "artifact") {
          patchReply((row) => ({ ...row, artifact_version_id: event.version_id }));
          setFocus({ type: "artifact", artifactId: event.artifact_id, versionId: event.version_id });
          void refreshWorkspace(id);
        } else if (event.type === "error") {
          patchReply((row) => ({ ...row, content: event.message, failed: true }));
        } else if (event.type === "done") {
          patchReply((row) => ({ ...row, id: event.message_id, pending: false }));
        }
      });
      await refreshWorkspace(id);
    } catch (reason) {
      patchReply((row) => ({
        ...row,
        pending: false,
        failed: true,
        content: reason instanceof Error ? reason.message : "The connection dropped before the reply finished.",
      }));
    } finally {
      setBusy(false);
      api.sessions().then(setSessions, () => {});
    }
  }

  async function revert(artifactId: string, versionId: string) {
    if (!activeId) return;
    try {
      const created = await api.revert(artifactId, versionId);
      await refreshWorkspace(activeId);
      setMessages(await api.messages(activeId));
      setFocus({ type: "artifact", artifactId, versionId: created.id });
    } catch (reason) {
      showError(reason);
    }
  }

  function openVersion(versionId: string) {
    const artifact = artifacts.find((a) => a.versions.some((v) => v.id === versionId));
    if (artifact) setFocus({ type: "artifact", artifactId: artifact.id, versionId });
  }

  return (
    <div className={`workspace ${railOpen ? "rail-open" : ""}`}>
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        email={email}
        health={health}
        onPick={(id) => {
          pickSession(id);
          setRailOpen(false);
        }}
        onNew={newSession}
        onDelete={removeSession}
        onSignOut={onSignOut}
      />
      <button className="rail-scrim" aria-label="Close menu" onClick={() => setRailOpen(false)} />

      <main className="talk">
        <header className="talk-head">
          <button className="rail-toggle" onClick={() => setRailOpen(true)}>
            Menu
          </button>
          <h1>{sessions.find((s) => s.id === activeId)?.title ?? "New session"}</h1>
        </header>
        {health && !health.llm.configured && (
          <p className="notice">
            No model is configured yet, so the assistant can't reply. Add a key to <code>backend/.env</code> and restart
            the API.
          </p>
        )}
        {error && (
          <p className="notice notice-error" role="alert">
            {error} <button onClick={() => setError(null)}>Dismiss</button>
          </p>
        )}
        <Transcript
          messages={messages}
          files={files}
          artifacts={artifacts}
          onOpenVersion={openVersion}
          onCite={setHighlightSource}
          onPrompt={(text) => void send(text, [])}
          sessionHasContent={files.length > 0}
        />
        <Composer key={composerKey} busy={busy} onSend={send} onUpload={upload} />
      </main>

      <Desk
        focus={focus}
        files={files}
        artifacts={artifacts}
        sources={sources}
        highlightSource={highlightSource}
        onFocus={setFocus}
        onRevert={revert}
      />
    </div>
  );
}

import { Fragment, useEffect, useRef, type ReactNode } from "react";

import type { Artifact, Message, StoredFile } from "../types";

type Props = {
  messages: Message[];
  files: StoredFile[];
  artifacts: Artifact[];
  sessionHasContent: boolean;
  onOpenVersion: (versionId: string) => void;
  onCite: (key: string) => void;
  onPrompt: (text: string) => void;
};

export default function Transcript({ messages, files, artifacts, sessionHasContent, onOpenVersion, onCite, onPrompt }: Props) {
  const end = useRef<HTMLDivElement>(null);
  const last = messages.at(-1);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, last?.content, last?.trace.length]);

  if (messages.length === 0) {
    return (
      <div className="transcript transcript-empty">
        <p className="empty-lede">
          {sessionHasContent
            ? "Your file is on the desk. Say what should change, or ask what's in it."
            : "Attach a Word, PowerPoint or Excel file and say what should change. Or just ask for a new one."}
        </p>
        <ul className="empty-prompts">
          <li>
            <button onClick={() => onPrompt("Write a one-page project update for the board as a Word document.")}>
              Write a one-page project update for the board
            </button>
          </li>
          <li>
            <button onClick={() => onPrompt("Make a five-slide deck on current EU rules for electric lorries, with sources.")}>
              Make a five-slide deck on EU rules for electric lorries, with sources
            </button>
          </li>
        </ul>
      </div>
    );
  }

  return (
    <div className="transcript" aria-live="polite">
      {messages.map((message) => (
        <article key={message.id} className={`entry entry-${message.role} ${message.failed ? "entry-failed" : ""}`}>
          <h2 className="entry-who">{message.role === "user" ? "You" : "Redline"}</h2>
          <div className="entry-body">
            {message.file_ids.length > 0 && (
              <p className="entry-files">
                {message.file_ids.map((id) => files.find((f) => f.id === id)?.filename ?? "attachment").join(", ")}
              </p>
            )}
            {message.trace.length > 0 && <WorkLog steps={message.trace} live={Boolean(message.pending)} />}
            {message.pending && !message.content && message.trace.length === 0 && <p className="thinking">Reading your message</p>}
            <Prose text={message.content} onCite={onCite} />
            {message.artifact_version_id && (
              <VersionLink versionId={message.artifact_version_id} artifacts={artifacts} onOpen={onOpenVersion} />
            )}
          </div>
        </article>
      ))}
      <div ref={end} />
    </div>
  );
}

function WorkLog({ steps, live }: { steps: { agent: string; detail: string }[]; live: boolean }) {
  return (
    <details className="worklog" open={live}>
      <summary>{live ? "Working" : `${steps.length} steps`}</summary>
      <ol>
        {steps.map((step, index) => (
          <li key={index}>
            <span className="worklog-agent">{step.agent}</span>
            <span>{step.detail}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

function VersionLink({
  versionId,
  artifacts,
  onOpen,
}: {
  versionId: string;
  artifacts: Artifact[];
  onOpen: (versionId: string) => void;
}) {
  const artifact = artifacts.find((a) => a.versions.some((v) => v.id === versionId));
  const version = artifact?.versions.find((v) => v.id === versionId);
  if (!artifact || !version) return null;
  return (
    <button className="version-link" onClick={() => onOpen(versionId)}>
      <span className="kind-tag">{artifact.kind}</span>
      {artifact.title}
      <span className="version-number">v{version.number}</span>
    </button>
  );
}

function Prose({ text, onCite }: { text: string; onCite: (key: string) => void }) {
  if (!text) return null;
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="prose">
      {blocks.map((block, index) => {
        const lines = block.split("\n");
        if (lines.every((line) => /^\s*([-*]|\d+\.)\s+/.test(line))) {
          const ordered = /^\s*\d+\./.test(lines[0]);
          const items = lines.map((line, i) => (
            <li key={i}>{inline(line.replace(/^\s*([-*]|\d+\.)\s+/, ""), onCite)}</li>
          ));
          return ordered ? <ol key={index}>{items}</ol> : <ul key={index}>{items}</ul>;
        }
        return (
          <p key={index}>
            {lines.map((line, i) => (
              <Fragment key={i}>
                {i > 0 && <br />}
                {inline(line.replace(/^#+\s+/, ""), onCite)}
              </Fragment>
            ))}
          </p>
        );
      })}
    </div>
  );
}

function inline(text: string, onCite: (key: string) => void): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[S\d+\])/g).map((piece, index) => {
    if (/^\*\*.+\*\*$/.test(piece)) return <strong key={index}>{piece.slice(2, -2)}</strong>;
    if (/^`.+`$/.test(piece)) return <code key={index}>{piece.slice(1, -1)}</code>;
    if (/^\[S\d+\]$/.test(piece)) {
      const key = piece.slice(1, -1);
      return (
        <button key={index} className="cite" title={`Show source ${key}`} onClick={() => onCite(key)}>
          {key}
        </button>
      );
    }
    return piece;
  });
}

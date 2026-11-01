import { useRef, useState, type DragEvent, type KeyboardEvent } from "react";

import type { StoredFile } from "../types";

const ACCEPT = ".docx,.pptx,.xlsx,.pdf,.txt,.md,.csv,.png,.jpg,.jpeg";

type Props = {
  busy: boolean;
  onSend: (text: string, fileIds: string[]) => void;
  onUpload: (file: File) => Promise<StoredFile>;
};

export default function Composer({ busy, onSend, onUpload }: Props) {
  const [text, setText] = useState("");
  const [attached, setAttached] = useState<StoredFile[]>([]);
  const [uploading, setUploading] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  async function addFiles(list: FileList | null) {
    if (!list) return;
    setProblem(null);
    for (const file of Array.from(list)) {
      setUploading((names) => [...names, file.name]);
      try {
        const stored = await onUpload(file);
        setAttached((rows) => [...rows, stored]);
        if (stored.index_status === "failed") {
          setProblem(`${stored.filename} can be edited, but searching inside it failed.`);
        }
      } catch (reason) {
        setProblem(`${file.name}: ${reason instanceof Error ? reason.message : "upload failed"}`);
      } finally {
        setUploading((names) => names.filter((name) => name !== file.name));
      }
    }
  }

  function submit() {
    const message = text.trim();
    if (!message || busy || uploading.length) return;
    onSend(
      message,
      attached.map((file) => file.id),
    );
    setText("");
    setAttached([]);
    setProblem(null);
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void addFiles(event.dataTransfer.files);
  }

  return (
    <div
      className={`composer ${dragging ? "is-dragging" : ""}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
    >
      {(attached.length > 0 || uploading.length > 0) && (
        <ul className="attachments">
          {attached.map((file) => (
            <li key={file.id}>
              {file.filename}
              <button
                aria-label={`Remove ${file.filename}`}
                onClick={() => setAttached((rows) => rows.filter((row) => row.id !== file.id))}
              >
                ×
              </button>
            </li>
          ))}
          {uploading.map((name) => (
            <li key={name} className="is-uploading">
              {name}, checking
            </li>
          ))}
        </ul>
      )}
      {problem && (
        <p className="form-error" role="alert">
          {problem}
        </p>
      )}
      <textarea
        rows={2}
        value={text}
        placeholder="Say what should change"
        aria-label="Message"
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="composer-row">
        <button className="link-button" onClick={() => picker.current?.click()}>
          Attach a file
        </button>
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          accept={ACCEPT}
          onChange={(event) => {
            void addFiles(event.target.files);
            event.target.value = "";
          }}
        />
        <span className="composer-hint">Enter sends, Shift+Enter starts a new line</span>
        <button className="button-primary" disabled={busy || !text.trim() || uploading.length > 0} onClick={submit}>
          {busy ? "Working" : "Send"}
        </button>
      </div>
    </div>
  );
}

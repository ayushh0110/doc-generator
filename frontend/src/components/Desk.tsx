import { useEffect, useState } from "react";

import { api } from "../api";
import { fileSize, shortDate } from "../format";
import type { Artifact, Citation, Preview, Profile, Source, StoredFile, VersionDetail } from "../types";
import StructureView from "./StructureView";

export type DeskFocus =
  | { type: "artifact"; artifactId: string; versionId: string }
  | { type: "file"; fileId: string }
  | null;

type Tab = "preview" | "changes" | "structure" | "sources" | "checks";

type Props = {
  focus: DeskFocus;
  files: StoredFile[];
  artifacts: Artifact[];
  sources: Source[];
  highlightSource: string | null;
  onFocus: (focus: DeskFocus) => void;
  onRevert: (artifactId: string, versionId: string) => void;
};

export default function Desk({ focus, files, artifacts, sources, highlightSource, onFocus, onRevert }: Props) {
  const [tab, setTab] = useState<Tab>("preview");
  const artifact = focus?.type === "artifact" ? artifacts.find((a) => a.id === focus.artifactId) : undefined;
  const version = focus?.type === "artifact" ? artifact?.versions.find((v) => v.id === focus.versionId) : undefined;
  const file = focus?.type === "file" ? files.find((f) => f.id === focus.fileId) : undefined;

  useEffect(() => {
    if (highlightSource) setTab("sources");
  }, [highlightSource]);

  useEffect(() => {
    if (focus?.type === "file") setTab("structure");
  }, [focus]);

  useEffect(() => {
    if (focus?.type === "artifact") setTab((current) => (current === "structure" ? current : "preview"));
    // Only when the document itself changes, not on every version switch.
  }, [focus?.type === "artifact" ? focus.artifactId : null]);

  const tabs: { id: Tab; label: string }[] = artifact
    ? [
        { id: "preview", label: "Preview" },
        { id: "changes", label: "Changes" },
        { id: "structure", label: "Structure" },
        { id: "sources", label: "Sources" },
        { id: "checks", label: "Checks" },
      ]
    : [
        { id: "structure", label: "Structure" },
        { id: "sources", label: "Sources" },
      ];
  const activeTab = tabs.some((t) => t.id === tab) ? tab : tabs[0].id;

  return (
    <section className="desk" aria-label="Document">
      <header className="desk-head">
        <label className="desk-picker">
          <span className="sr-only">Document</span>
          <select
            value={
              focus?.type === "artifact" ? `artifact:${focus.artifactId}` : focus?.type === "file" ? `file:${focus.fileId}` : ""
            }
            onChange={(event) => {
              const [type, id] = event.target.value.split(":");
              if (type === "file") onFocus({ type: "file", fileId: id });
              else {
                const next = artifacts.find((a) => a.id === id);
                if (next) onFocus({ type: "artifact", artifactId: id, versionId: next.versions.at(-1)!.id });
              }
            }}
          >
            {!focus && <option value="">Nothing open</option>}
            {artifacts.length > 0 && (
              <optgroup label="Generated">
                {artifacts.map((item) => (
                  <option key={item.id} value={`artifact:${item.id}`}>
                    {item.title} ({item.kind})
                  </option>
                ))}
              </optgroup>
            )}
            {files.length > 0 && (
              <optgroup label="Uploaded">
                {files.map((item) => (
                  <option key={item.id} value={`file:${item.id}`} disabled={!item.has_profile}>
                    {item.filename}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        {version && artifact && (
          <button className="button-quiet" onClick={() => void api.download(`/versions/${version.id}/download`)}>
            Download .{artifact.kind}
          </button>
        )}
        {file && (
          <button className="button-quiet" onClick={() => void api.download(`/files/${file.id}/download`)}>
            Download original
          </button>
        )}
      </header>

      {artifact && version && (
        <div className="versions" role="group" aria-label="Versions">
          {artifact.versions.map((item) => (
            <button
              key={item.id}
              className={item.id === version.id ? "is-current" : ""}
              title={item.instruction}
              onClick={() => onFocus({ type: "artifact", artifactId: artifact.id, versionId: item.id })}
            >
              v{item.number}
            </button>
          ))}
          {version.id !== artifact.versions.at(-1)!.id && (
            <button className="versions-restore" onClick={() => onRevert(artifact.id, version.id)}>
              Restore v{version.number}
            </button>
          )}
          <span className="versions-note">{version.summary}</span>
        </div>
      )}

      {file && (
        <p className="desk-meta">
          {file.kind.toUpperCase()}, {fileSize(file.size_bytes)}, uploaded {shortDate(file.created_at)}
          {file.warnings.map((warning) => (
            <span key={warning} className="desk-warning">
              {warning}
            </span>
          ))}
        </p>
      )}

      {focus ? (
        <>
          <div className="tabs" role="tablist">
            {tabs.map((item) => (
              <button
                key={item.id}
                role="tab"
                aria-selected={item.id === activeTab}
                className={item.id === activeTab ? "is-active" : ""}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <div className="tab-body">
            {activeTab === "preview" && version && <PreviewTab versionId={version.id} />}
            {activeTab === "changes" && version && <ChangesTab versionId={version.id} />}
            {activeTab === "checks" && version && <ChecksTab versionId={version.id} />}
            {activeTab === "structure" && <StructureTab versionId={version?.id} fileId={file?.id} />}
            {activeTab === "sources" && <SourcesTab versionId={version?.id} sources={sources} highlight={highlightSource} />}
          </div>
        </>
      ) : (
        <div className="desk-empty">
          <p>Documents open here, with every version, what changed and where the facts came from.</p>
        </div>
      )}
    </section>
  );
}

function useLoad<T>(load: () => Promise<T>, key: unknown): { data: T | null; error: string | null } {
  const [state, setState] = useState<{ key: unknown; data: T | null; error: string | null }>({
    key: null,
    data: null,
    error: null,
  });
  useEffect(() => {
    let live = true;
    load().then(
      (data) => live && setState({ key, data, error: null }),
      (reason) => live && setState({ key, data: null, error: reason instanceof Error ? reason.message : "Could not load this" }),
    );
    return () => {
      live = false;
    };
  }, [key]);
  return state.key === key ? state : { data: null, error: null };
}

function PreviewTab({ versionId }: { versionId: string }) {
  const { data, error } = useLoad<Preview>(() => api.preview(versionId), versionId);
  if (error) return <p className="tab-note">{error}</p>;
  if (!data) return <p className="tab-note">Rendering the pages. This can take a few seconds.</p>;
  if (!data.available) return <p className="tab-note">Can't show a preview: {data.reason}. The download still works.</p>;
  return (
    <div className="pages">
      {Array.from({ length: data.pages }, (_, index) => (
        <PageImage key={`${versionId}-${index}`} versionId={versionId} page={index + 1} />
      ))}
    </div>
  );
}

function PageImage({ versionId, page }: { versionId: string; page: number }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    let created: string | null = null;
    api.previewImage(versionId, page).then((next) => {
      created = next;
      if (live) setUrl(next);
      else URL.revokeObjectURL(next);
    });
    return () => {
      live = false;
      if (created) URL.revokeObjectURL(created);
    };
  }, [versionId, page]);
  return (
    <figure className="page">
      {url ? <img src={url} alt={`Page ${page}`} /> : <div className="page-loading" />}
      <figcaption>{page}</figcaption>
    </figure>
  );
}

function ChangesTab({ versionId }: { versionId: string }) {
  const { data, error } = useLoad<VersionDetail>(() => api.version(versionId), versionId);
  if (error) return <p className="tab-note">{error}</p>;
  if (!data) return null;
  if (data.changes.length === 0) return <p className="tab-note">No text changed in this version.</p>;
  return (
    <ol className="changes">
      {data.changes.map((change, index) => (
        <li key={index} className={`change change-${change.kind}`}>
          <span className="change-where">{change.element}</span>
          {change.before !== null && <del>{change.before}</del>}
          {change.after !== null && <ins>{change.after}</ins>}
        </li>
      ))}
    </ol>
  );
}

function ChecksTab({ versionId }: { versionId: string }) {
  const { data, error } = useLoad<VersionDetail>(() => api.version(versionId), versionId);
  if (error) return <p className="tab-note">{error}</p>;
  if (!data) return null;
  if (data.checks.length === 0) return <p className="tab-note">This version was restored, so it carries no new checks.</p>;
  return (
    <ul className="checks">
      {data.checks.map((check) => (
        <li key={check.name} className={check.ok ? "check-ok" : "check-bad"}>
          <span className="check-mark">{check.ok ? "Pass" : "Fail"}</span>
          <span className="check-name">{check.name}</span>
          {check.detail && <span className="check-detail">{check.detail}</span>}
        </li>
      ))}
    </ul>
  );
}

function StructureTab({ versionId, fileId }: { versionId?: string; fileId?: string }) {
  const key = versionId ?? fileId;
  const { data, error } = useLoad<Profile>(() => (versionId ? api.structure(versionId) : api.fileAnalysis(fileId!)), key);
  if (error) return <p className="tab-note">{error}</p>;
  if (!data) return <p className="tab-note">Reading the file.</p>;
  return <StructureView profile={data} />;
}

function SourcesTab({
  versionId,
  sources,
  highlight,
}: {
  versionId?: string;
  sources: Source[];
  highlight: string | null;
}) {
  const { data: citations } = useLoad<Citation[]>(() => (versionId ? api.citations(versionId) : Promise.resolve([])), versionId ?? "none");
  useEffect(() => {
    if (highlight) document.getElementById(`source-${highlight}`)?.scrollIntoView({ block: "center" });
  }, [highlight]);

  if (sources.length === 0) return <p className="tab-note">No sources yet. They appear when a turn searches your files or the web.</p>;
  return (
    <ul className="sources">
      {sources.map((source) => {
        const uses = (citations ?? []).filter((c) => c.source.id === source.id);
        return (
          <li key={source.id} id={`source-${source.key}`} className={highlight === source.key ? "is-highlighted" : ""}>
            <p className="source-head">
              <span className="source-key">{source.key}</span>
              {source.url ? (
                <a href={source.url} target="_blank" rel="noreferrer noopener">
                  {source.title}
                </a>
              ) : (
                <span>{source.title}</span>
              )}
            </p>
            <p className="source-where">
              {source.kind === "web" ? "Web page" : source.locator}, retrieved {shortDate(source.retrieved_at)}
            </p>
            <blockquote>{source.snippet}</blockquote>
            {uses.length > 0 && (
              <div className="source-uses">
                <p>Used in this version at</p>
                <ul>
                  {uses.map((use, index) => (
                    <li key={index}>
                      <span className="change-where">{use.element}</span> {use.excerpt}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

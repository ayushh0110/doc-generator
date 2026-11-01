import type { ChatSession, Health } from "../types";
import { shortDate } from "../format";

type Props = {
  sessions: ChatSession[];
  activeId: string | null;
  email: string;
  health: Health | null;
  onPick: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onSignOut: () => void;
};

export default function Sidebar({ sessions, activeId, email, health, onPick, onNew, onDelete, onSignOut }: Props) {
  return (
    <aside className="rail">
      <p className="wordmark">
        Red<span>line</span>
      </p>
      <button className="new-session" onClick={onNew}>
        New session
      </button>

      <nav className="session-list" aria-label="Sessions">
        {sessions.length === 0 && <p className="rail-empty">Nothing here yet.</p>}
        {sessions.map((item) => (
          <div key={item.id} className={`session-row ${item.id === activeId ? "is-active" : ""}`}>
            <button className="session-pick" onClick={() => onPick(item.id)}>
              <span className="session-title">{item.title}</span>
              <span className="session-date">{shortDate(item.updated_at)}</span>
            </button>
            <button
              className="session-delete"
              aria-label={`Delete ${item.title}`}
              title="Delete session"
              onClick={() => {
                if (window.confirm(`Delete "${item.title}" and its files?`)) onDelete(item.id);
              }}
            >
              ×
            </button>
          </div>
        ))}
      </nav>

      <footer className="rail-foot">
        {health && (
          <dl className="system">
            <div>
              <dt>Model</dt>
              <dd>{health.llm.configured ? health.llm.model : "not set"}</dd>
            </div>
            <div>
              <dt>Search</dt>
              <dd>{health.search}</dd>
            </div>
            <div>
              <dt>Web</dt>
              <dd>{health.web}</dd>
            </div>
          </dl>
        )}
        <p className="rail-email" title={email}>
          {email}
        </p>
        <button className="link-button" onClick={onSignOut}>
          Sign out
        </button>
      </footer>
    </aside>
  );
}

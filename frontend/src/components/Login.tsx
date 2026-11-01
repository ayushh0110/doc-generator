import { useState, type FormEvent } from "react";

import { api, session } from "../api";

export default function Login({ onDone }: { onDone: (email: string) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setWaiting(true);
    try {
      const result = await (mode === "login" ? api.login(email, password) : api.register(email, password));
      session.save(result.token, result.email);
      onDone(result.email);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not sign in");
    } finally {
      setWaiting(false);
    }
  }

  return (
    <div className="login">
      <div className="login-copy">
        <p className="wordmark wordmark-large">
          Red<span>line</span>
        </p>
        <p className="login-lede">
          Hand it your Word, PowerPoint or Excel file and say what should change. It keeps the letterhead, the slide
          master and the formulas exactly where they were.
        </p>
      </div>

      <form className="login-form" onSubmit={submit}>
        <h1>{mode === "login" ? "Sign in" : "Create an account"}</h1>
        <label>
          Email
          <input type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            required
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {mode === "register" && <p className="hint">At least 8 characters.</p>}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button className="button-primary" disabled={waiting}>
          {waiting ? "One moment" : mode === "login" ? "Sign in" : "Create account"}
        </button>
        <button
          type="button"
          className="link-button"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
        >
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}

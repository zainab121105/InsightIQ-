import React, { useState } from "react";
import { useAuth } from "./AuthProvider";

export function LoginPage({ onSignup }) {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await login({ email: email.trim(), password });
    } catch (loginError) {
      setError(loginError.message || "Unable to sign in.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-card surface">
        <div className="brand auth-brand"><div className="brand-mark">✦</div><div><b>InsightIQ</b><span>Your Business. Your Data. Your Intelligence.</span></div></div>
        <span className="eyebrow">WELCOME BACK</span>
        <h1>Sign in to your workspace</h1>
        <p className="auth-subtitle">Continue exploring your business intelligence.</p>
        {error && <div className="error-box">{error}</div>}
        <form className="auth-form" onSubmit={submit}>
          <label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required /></label>
          <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
          <button className="violet-button" type="submit" disabled={submitting}>{submitting ? "Signing in..." : "Sign in"}</button>
        </form>
        <p className="auth-switch">New to InsightIQ? <button type="button" onClick={onSignup}>Create an account</button></p>
      </section>
    </main>
  );
}

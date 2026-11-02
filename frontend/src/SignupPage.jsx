import React, { useState } from "react";
import { useAuth } from "./AuthProvider";

export function SignupPage({ onLogin }) {
  const { signup } = useAuth();
  const [form, setForm] = useState({
    email: "", password: "", display_name: "", business_name: "", business_slug: "",
  });
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const update = (field) => (event) => setForm((current) => ({ ...current, [field]: event.target.value }));

  const submit = async (event) => {
    event.preventDefault();
    if (Object.values(form).some((value) => !value.trim())) {
      setError("Complete every field to create your workspace.");
      return;
    }
    if (form.password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await signup({ ...form, email: form.email.trim(), display_name: form.display_name.trim(), business_name: form.business_name.trim(), business_slug: form.business_slug.trim() });
    } catch (signupError) {
      setError(signupError.message || "Unable to create your account.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-card surface">
        <div className="brand auth-brand"><div className="brand-mark">✦</div><div><b>InsightIQ</b><span>Your Business. Your Data. Your Intelligence.</span></div></div>
        <span className="eyebrow">GET STARTED</span>
        <h1>Create your workspace</h1>
        <p className="auth-subtitle">Set up your business intelligence workspace in minutes.</p>
        {error && <div className="error-box">{error}</div>}
        <form className="auth-form" onSubmit={submit}>
          <label>Your name<input value={form.display_name} onChange={update("display_name")} autoComplete="name" required /></label>
          <label>Email<input type="email" value={form.email} onChange={update("email")} autoComplete="email" required /></label>
          <label>Password<input type="password" value={form.password} onChange={update("password")} autoComplete="new-password" minLength="8" required /></label>
          <label>Business name<input value={form.business_name} onChange={update("business_name")} required /></label>
          <label>Business slug<input value={form.business_slug} onChange={update("business_slug")} placeholder="my-business" required /></label>
          <button className="violet-button" type="submit" disabled={submitting}>{submitting ? "Creating workspace..." : "Create account"}</button>
        </form>
        <p className="auth-switch">Already have an account? <button type="button" onClick={onLogin}>Sign in</button></p>
      </section>
    </main>
  );
}

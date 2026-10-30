import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  askAssistant,
  getMonthly,
  getProducts,
  getRegions,
  getSummary,
} from "./api";
import { SalesDataManagement } from "./SalesDataManagement";

const PROMPTS = [
  "Which product generates the highest revenue?",
  "Which region performs best?",
  "Which month recorded the highest revenue?",
  "Compare product revenue.",
  "Give me an overall business summary.",
];

const NAV_ITEMS = [
  ["AI Assistant", "✦"],
  ["Overview", "⌂"],
  ["Analytics", "◒"],
  ["Sales Data", "⊞"],
  ["Reports", "▤"],
];

const defaultSettings = { theme: "dark", showResponseTime: true, contextOpen: true };

function money(value) {
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(Number(value || 0));
}

function number(value) {
  return new Intl.NumberFormat("en-IN").format(Number(value || 0));
}

function readStorage(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback));
  } catch {
    return fallback;
  }
}

function createChatId() {
  return typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function completedHistory(messages) {
  return messages
    .filter((message) =>
      (message?.role === "user" || message?.role === "assistant") &&
      typeof message.content === "string" &&
      message.content.trim()
    )
    .map(({ role, content }) => ({ role, content }))
    .slice(-10);
}

function downloadCsv(filename, rows) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(","),
    ...rows.map((row) => headers.map((header) => `"${String(row[header] ?? "").replaceAll('"', '""')}"`).join(",")),
  ].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function KpiCard({ label, value, detail, icon }) {
  return (
    <article className="kpi-card">
      <div className="kpi-icon">{icon}</div>
      <div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
    </article>
  );
}

function LoadingState({ label = "Loading live data..." }) {
  return <div className="state-box"><span className="loader" />{label}</div>;
}

function EmptyState({ label = "No data available yet." }) {
  return <div className="state-box muted">{label}</div>;
}

function BarChart({ data, nameKey }) {
  if (!data.length) return <EmptyState />;
  const max = Math.max(...data.map((item) => Number(item.total_revenue || 0)), 1);
  return <div className="bars">
    {data.map((item) => <div className="bar-item" key={item[nameKey]}>
      <div className="bar-label"><span title={item[nameKey]}>{item[nameKey]}</span><b>{money(item.total_revenue)}</b></div>
      <div className="bar-track"><i style={{ width: `${Math.max(3, (item.total_revenue / max) * 100)}%` }} /></div>
    </div>)}
  </div>;
}

function TrendChart({ data }) {
  if (!data.length) return <EmptyState />;
  const max = Math.max(...data.map((item) => Number(item.total_revenue || 0)), 1);
  const points = data.map((item, index) => {
    const x = data.length === 1 ? 50 : (index / (data.length - 1)) * 100;
    const y = 90 - (Number(item.total_revenue || 0) / max) * 72;
    return `${x},${y}`;
  }).join(" ");
  return <div className="trend-wrap">
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Monthly revenue trend">
      <polyline points={points} className="trend-line" />
      {data.map((item, index) => {
        const [x, y] = points.split(" ")[index].split(",");
        return <circle key={item.order_month} cx={x} cy={y} r="1.5" />;
      })}
    </svg>
    <div className="trend-labels">{data.map((item) => <span key={item.order_month}>{item.order_month}</span>)}</div>
  </div>;
}

function Sidebar({ page, setPage, onNewChat, chats, openChat, onSettings, mobileOpen, closeMobile }) {
  return <aside className={`sidebar ${mobileOpen ? "mobile-open" : ""}`}>
    <div>
      <div className="brand"><div className="brand-mark">✦</div><div><b>InsightIQ</b><span>Your Business. Your Data. Your Intelligence.</span></div></div>
      <button className="new-chat" onClick={onNewChat}>＋ <span>New conversation</span></button>
      <nav className="primary-nav">
        <p>Workspace</p>
        {NAV_ITEMS.map(([label, icon]) => <button key={label} className={page === label ? "active" : ""} onClick={() => { setPage(label); closeMobile(); }}><i>{icon}</i>{label}</button>)}
      </nav>
      <div className="recent-list"><p>Recent conversations</p>
        {chats.length ? chats.map((chat) => <button key={chat.id} onClick={() => { openChat(chat.id); closeMobile(); }}><i>◌</i>{chat.title}</button>) : <span className="empty-recent">No conversations yet</span>}
      </div>
    </div>
    <div className="sidebar-bottom">
      <button className="settings-link" onClick={() => { onSettings(); closeMobile(); }}><i>⚙</i> Settings</button>
      <div className="profile"><div className="avatar">ZA</div><div><b>Zainab Amreen</b><span>Business Analyst</span></div><button onClick={onSettings}>•••</button></div>
    </div>
  </aside>;
}

function Topbar({ page, onMenu, onNewChat, onToggleContext, contextOpen }) {
  return <header className="topbar"><button className="mobile-menu" onClick={onMenu}>☰</button><div className="crumb">Workspace <b>/</b> {page}</div><div className="top-actions"><span className="online"><i /> Online</span><button className="context-toggle" onClick={onToggleContext}>{contextOpen ? "Hide context" : "Show context"}</button><button className="top-new" onClick={onNewChat}>＋ New chat</button></div></header>;
}

function ChatMessage({ message, showTiming }) {
  return <div className={`chat-message ${message.role}`}><div className="message-avatar">{message.role === "user" ? "ZA" : "✦"}</div><div className="message-body"><span className="message-name">{message.role === "user" ? "You" : "InsightIQ"}</span>{message.role === "assistant" ? <div className="markdown-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div> : <p>{message.content}</p>}{showTiming && message.timings && <small>{message.timings.total_ms}ms response</small>}</div></div>;
}

function ChatWorkspace({ summary, messages, setMessages, activeChatId, setActiveChatId, chats, setChats, settings, showToast }) {
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottom = useRef(null);
  const activeChatIdRef = useRef(activeChatId);
  useEffect(() => {
    activeChatIdRef.current = activeChatId;
  }, [activeChatId]);
  useEffect(() => {
    if (bottom.current) {
      bottom.current.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, loading]);

  const submit = async (value) => {
    const question = value.trim();
    if (!question || loading) return;
    const chatId = activeChatId || createChatId();
    activeChatIdRef.current = chatId;
    if (!activeChatId) setActiveChatId(chatId);
    const next = [...messages, { role: "user", content: question }];
    setMessages(next); setInput(""); setLoading(true);
    try {
      const previousMessages = completedHistory(messages);
      const result = await askAssistant(question, chatId, previousMessages);
      const completed = [...next, { role: "assistant", content: result.answer, timings: result.timings_ms }];
      if (activeChatIdRef.current === chatId) setMessages(completed);
      setChats((current) => {
        const title = question.length > 44 ? `${question.slice(0, 44)}…` : question;
        const existing = current.find((chat) => chat.id === chatId);
        return existing
          ? current.map((chat) => chat.id === chatId ? { ...chat, messages: completed } : chat)
          : [{ id: chatId, title, messages: completed }, ...current].slice(0, 15);
      });
    } catch (error) {
      if (activeChatIdRef.current === chatId) {
        setMessages(next);
      }
      showToast(error.message || "The assistant could not complete that request.");
    } finally { setLoading(false); }
  };

  const keyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(input); }
  };

  return <section className="assistant-page">
    <div className="assistant-heading"><div><span className="eyebrow">AI BUSINESS ASSISTANT</span><h1>Your business data,<br /><em>at your fingertips.</em></h1><p>Ask questions. Discover patterns. Make smarter decisions.</p></div><div className="ai-orbit">✦</div></div>
    <div className="kpi-grid">
      <KpiCard label="Total revenue" value={summary ? money(summary.total_revenue) : "—"} detail="Across all transactions" icon="₹" />
      <KpiCard label="Transactions" value={summary ? number(summary.total_transactions) : "—"} detail="Completed records" icon="↗" />
      <KpiCard label="Quantity sold" value={summary ? number(summary.total_quantity) : "—"} detail="Units across products" icon="▦" />
      <KpiCard label="Avg. per transaction" value={summary ? money(summary.average_revenue) : "—"} detail="Average order value" icon="⌁" />
    </div>
    <div className="conversation">
      <div className="conversation-head"><div className="assistant-identity"><div className="mini-orb">✦</div><div><b>InsightIQ Assistant</b><span>Grounded in your business data</span></div></div><span className="secure">● Secure session</span></div>
      <div className="message-list">{messages.length === 0 ? <div className="welcome-chat"><div className="welcome-icon">✦</div><h2>What would you like to understand?</h2><p>Ask about revenue, products, regions, trends or say hello.</p></div> : messages.map((message, index) => <ChatMessage key={`${message.role}-${index}`} message={message} showTiming={settings.showResponseTime} />)}{loading && <div className="chat-message assistant"><div className="message-avatar">✦</div><div className="message-body"><span className="message-name">InsightIQ</span><div className="thinking"><i /><i /><i /> Analyzing your business data...</div></div></div>}<div ref={bottom} /></div>
      <div className="prompt-row">{PROMPTS.map((prompt) => <button key={prompt} onClick={() => submit(prompt)} disabled={loading}>{prompt}</button>)}</div>
      <div className="chat-input"><textarea value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={keyDown} rows="1" placeholder="Ask anything about your business data..." aria-label="Ask anything about your business data" /><button onClick={() => submit(input)} disabled={loading || !input.trim()} aria-label="Send message">↑</button></div><small className="keyboard-hint">Press Enter to send <span>•</span> Shift + Enter for a new line</small>
    </div>
  </section>;
}

function InsightPanel({ products, regions, monthly, summary, open }) {
  const bestMonth = monthly.reduce((top, item) => !top || item.total_revenue > top.total_revenue ? item : top, null);
  return <aside className={`context-panel ${open ? "" : "closed"}`}><div className="context-title"><div><span className="eyebrow">CONTEXT</span><h2>Business pulse</h2></div><span className="pulse-dot" /></div><div className="insight-stack"><div className="insight-card violet"><span>Top revenue product</span><b>{products[0]?.product || "—"}</b><small>{products[0] ? money(products[0].total_revenue) : "Waiting for data"}</small></div><div className="insight-card green"><span>Best performing region</span><b>{regions[0]?.region || "—"}</b><small>{regions[0] ? money(regions[0].total_revenue) : "Waiting for data"}</small></div><div className="insight-card amber"><span>Highest revenue month</span><b>{bestMonth?.order_month || "—"}</b><small>{bestMonth ? money(bestMonth.total_revenue) : "Waiting for data"}</small></div></div><div className="context-summary"><span>Latest summary</span><p>{summary ? `${number(summary.total_transactions)} transactions generated ${money(summary.total_revenue)} in revenue.` : "Loading your latest business summary..."}</p></div><div className="context-note">✦ Insights update whenever your analytics refresh.</div></aside>;
}

function Overview({ summary, products, regions, monthly, loading, onNavigate }) {
  const bestMonth = monthly.reduce((top, item) => !top || item.total_revenue > top.total_revenue ? item : top, null);
  return <PageShell eyebrow="OVERVIEW" title="Business performance" description="A clear view of what is happening across your business today." action={<button className="violet-button" onClick={() => onNavigate("Analytics")}>Explore analytics →</button>}>
    {loading ? <LoadingState /> : <><div className="kpi-grid overview-kpis"><KpiCard label="Total revenue" value={money(summary?.total_revenue)} detail="All recorded sales" icon="₹" /><KpiCard label="Transactions" value={number(summary?.total_transactions)} detail="Completed records" icon="↗" /><KpiCard label="Quantity sold" value={number(summary?.total_quantity)} detail="Units sold" icon="▦" /><KpiCard label="Average transaction" value={money(summary?.average_revenue)} detail="Average order value" icon="⌁" /></div><div className="overview-grid"><div className="surface"><div className="section-head"><div><h2>Revenue highlights</h2><span>Leaders across your business</span></div></div><div className="highlight-grid"><div><span>Top product</span><b>{products[0]?.product || "—"}</b><small>{products[0] ? money(products[0].total_revenue) : "—"}</small></div><div><span>Top region</span><b>{regions[0]?.region || "—"}</b><small>{regions[0] ? money(regions[0].total_revenue) : "—"}</small></div><div><span>Best month</span><b>{bestMonth?.order_month || "—"}</b><small>{bestMonth ? money(bestMonth.total_revenue) : "—"}</small></div></div></div><div className="surface"><div className="section-head"><div><h2>Product performance</h2><span>Top revenue products</span></div><button className="link-button" onClick={() => onNavigate("Analytics")}>View all →</button></div><BarChart data={products.slice(0, 4)} nameKey="product" /></div></div></>}
  </PageShell>;
}

function PageShell({ eyebrow, title, description, action, children }) {
  return <section className="page-shell"><div className="page-heading"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</div>{children}</section>;
}

function Analytics({ products, regions, monthly, loading, error, refresh }) {
  return <PageShell eyebrow="ANALYTICS" title="Revenue analytics" description="Explore performance across products, regions and time." action={<button className="violet-button" onClick={refresh}>↻ Refresh data</button>}>
    {error && <div className="error-box">{error}<button onClick={refresh}>Retry</button></div>}{loading ? <LoadingState /> : <div className="analytics-grid"><div className="surface"><div className="section-head"><div><h2>Product revenue</h2><span>Revenue by product</span></div></div><BarChart data={products} nameKey="product" /></div><div className="surface"><div className="section-head"><div><h2>Regional revenue</h2><span>Revenue by region</span></div></div><BarChart data={regions} nameKey="region" /></div><div className="surface wide"><div className="section-head"><div><h2>Monthly revenue trend</h2><span>Revenue over time</span></div></div><TrendChart data={monthly} /></div></div>}
  </PageShell>;
}

function Reports({ summary, products, regions, monthly }) {
  const exportAll = () => downloadCsv("insightiq-analytics.csv", [...products.map((row) => ({ category: "Product", ...row })), ...regions.map((row) => ({ category: "Region", ...row })), ...monthly.map((row) => ({ category: "Month", ...row }))]);
  return <PageShell eyebrow="REPORTS" title="Business reports" description="Review and export the latest analytics available from your workspace." action={<button className="violet-button" onClick={exportAll}>↓ Export CSV</button>}><div className="report-summary surface"><div><span>Total revenue</span><b>{money(summary?.total_revenue)}</b></div><div><span>Transactions</span><b>{number(summary?.total_transactions)}</b></div><div><span>Quantity sold</span><b>{number(summary?.total_quantity)}</b></div><div><span>Average transaction</span><b>{money(summary?.average_revenue)}</b></div></div><div className="report-tables"><div className="surface"><div className="section-head"><h2>Product performance</h2><button className="link-button" onClick={() => downloadCsv("product-revenue.csv", products)}>Export</button></div><DataTable rows={products} nameKey="product" /></div><div className="surface"><div className="section-head"><h2>Regional performance</h2><button className="link-button" onClick={() => downloadCsv("regional-revenue.csv", regions)}>Export</button></div><DataTable rows={regions} nameKey="region" /></div></div><div className="surface"><div className="section-head"><h2>Monthly performance</h2><button className="link-button" onClick={() => downloadCsv("monthly-revenue.csv", monthly)}>Export</button></div><DataTable rows={monthly} nameKey="order_month" /></div></PageShell>;
}

function DataTable({ rows, nameKey }) {
  if (!rows.length) return <EmptyState />;
  return <div className="data-table">{rows.map((row) => <div key={row[nameKey]}><span>{row[nameKey]}</span><b>{money(row.total_revenue)}</b></div>)}</div>;
}

function Settings({ settings, setSettings, clearHistory, resetSettings }) {
  return <PageShell eyebrow="SETTINGS" title="Workspace preferences" description="Customize your InsightIQ experience. Preferences are saved on this device."><div className="settings-card surface"><label><span><b>Theme preference</b><small>Dark mode is optimized for focused analysis.</small></span><select value={settings.theme} onChange={(event) => setSettings({ ...settings, theme: event.target.value })}><option value="dark">Dark</option><option value="light">Light</option></select></label><label><span><b>Response timing</b><small>Show the time taken for each AI answer.</small></span><input type="checkbox" checked={settings.showResponseTime} onChange={(event) => setSettings({ ...settings, showResponseTime: event.target.checked })} /></label><label><span><b>Context panel</b><small>Show live business insights beside the assistant.</small></span><input type="checkbox" checked={settings.contextOpen} onChange={(event) => setSettings({ ...settings, contextOpen: event.target.checked })} /></label><div className="settings-actions"><button className="danger-button" onClick={clearHistory}>Clear conversation history</button><button className="outline-button" onClick={resetSettings}>Reset preferences</button></div></div></PageShell>;
}

function App() {
  const [page, setPage] = useState("AI Assistant");
  const [mobileOpen, setMobileOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(true);
  const [summary, setSummary] = useState(null);
  const [products, setProducts] = useState([]);
  const [regions, setRegions] = useState([]);
  const [monthly, setMonthly] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [messages, setMessages] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  const [chats, setChats] = useState(() => readStorage("insightiq-chats", []));
  const [settings, setSettings] = useState(() => ({ ...defaultSettings, ...readStorage("insightiq-settings", {}) }));
  const [toast, setToast] = useState("");

  useEffect(() => { localStorage.setItem("insightiq-chats", JSON.stringify(chats)); }, [chats]);
  useEffect(() => { localStorage.setItem("insightiq-settings", JSON.stringify(settings)); setContextOpen(settings.contextOpen); document.documentElement.dataset.theme = settings.theme; }, [settings]);
  const showToast = useCallback((message) => { setToast(message); window.setTimeout(() => setToast(""), 3000); }, []);
  const loadData = useCallback(async () => {
    setLoading(true); setError("");
    try { const [s, p, r, m] = await Promise.all([getSummary(), getProducts(), getRegions(), getMonthly()]); setSummary(s); setProducts(p); setRegions(r); setMonthly(m); }
    catch (err) { setError(err.message); showToast("Analytics could not be refreshed."); }
    finally { setLoading(false); }
  }, [showToast]);
  useEffect(() => { loadData(); }, [loadData]);
  const newChat = () => { setActiveChatId(createChatId()); setMessages([]); setPage("AI Assistant"); showToast("Started a new conversation."); };
  const openChat = (id) => { const chat = chats.find((item) => item.id === id); if (chat) { setActiveChatId(chat.id); setMessages(chat.messages); setPage("AI Assistant"); } };
  const clearHistory = () => { if (window.confirm("Clear all saved conversations? This cannot be undone.")) { setChats([]); setActiveChatId(null); setMessages([]); showToast("Conversation history cleared."); } };
  const resetSettings = () => { setSettings(defaultSettings); showToast("Preferences reset."); };
  const pageContent =
    page === "AI Assistant" ? (
      <ChatWorkspace
        summary={summary}
        messages={messages}
        setMessages={setMessages}
        activeChatId={activeChatId}
        setActiveChatId={setActiveChatId}
        chats={chats}
        setChats={setChats}
        settings={settings}
        showToast={showToast}
      />
    ) : page === "Overview" ? (
      <Overview
        summary={summary}
        products={products}
        regions={regions}
        monthly={monthly}
        loading={loading}
        onNavigate={setPage}
      />
    ) : page === "Analytics" ? (
      <Analytics
        products={products}
        regions={regions}
        monthly={monthly}
        loading={loading}
        error={error}
        refresh={loadData}
      />
    ) : page === "Sales Data" ? (
      <SalesDataManagement
        onImportSuccess={loadData}
        showToast={showToast}
      />
    ) : page === "Reports" ? (
      <Reports
        summary={summary}
        products={products}
        regions={regions}
        monthly={monthly}
      />
    ) : (
      <Settings
        settings={settings}
        setSettings={setSettings}
        clearHistory={clearHistory}
        resetSettings={resetSettings}
      />
    );
  return <div className={`app-shell ${page === "AI Assistant" && contextOpen ? "with-context" : ""}`}><Sidebar page={page} setPage={setPage} onNewChat={newChat} chats={chats} openChat={openChat} onSettings={() => setPage("Settings")} mobileOpen={mobileOpen} closeMobile={() => setMobileOpen(false)} /><main className="main-content"><Topbar page={page} onMenu={() => setMobileOpen(!mobileOpen)} onNewChat={newChat} onToggleContext={() => setContextOpen(!contextOpen)} contextOpen={contextOpen} /><div className="workspace">{pageContent}{toast && <div className="toast" role="status">{toast}</div>}</div></main>{page === "AI Assistant" && <InsightPanel products={products} regions={regions} monthly={monthly} summary={summary} open={contextOpen} />}</div>;
}

export default App;

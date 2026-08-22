"use client";

import { useEffect, useRef, useState } from "react";
import { usePlaidLink } from "react-plaid-link";
import AppNav from "@/components/AppNav";
import FloatingNotice from "@/components/FloatingNotice";
import FloatingSaveBar from "@/components/FloatingSaveBar";
import {
  API_BASE,
  apiFetch,
  AutomationStatus,
  IntegrationSettings,
  PortfolioOverview,
  RiskSummary,
  SetupStatus,
} from "@/lib/api";
import {
  safeFormatCurrency,
  formatRegimeLabel
} from "@/lib/formatting";
import { ArrowRight, Clock, Database, Landmark, Zap, Search, Shield, Trash2, CheckCircle2, AlertCircle, Terminal } from "lucide-react";
import LiveLogConsole from "@/components/LiveLogConsole";

type SettingsTab = "overview" | "data" | "research" | "system";
type GmailTestResult = {
  matched_messages: number;
};
type GmailSyncResult = {
  transactions_created?: number;
  processed_messages?: number;
  status?: string;
  detail?: string;
};

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>("overview");
  const [settings, setSettings] = useState<IntegrationSettings | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);
  const [automation, setAutomation] = useState<AutomationStatus | null>(null);
  const [portfolioOverview, setPortfolioOverview] = useState<PortfolioOverview | null>(null);
  const [riskSummary, setRiskSummary] = useState<RiskSummary | null>(null);

  const [isDirty, setIsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Gmail Form State
  const [gmailPassword, setGmailPassword] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [researchApiKey, setResearchApiKey] = useState("");
  const [plaidClientId, setPlaidClientId] = useState("");
  const [plaidSecret, setPlaidSecret] = useState("");

  // Reset State
  const [resetPhrase, setResetPhrase] = useState("");
  const [resetBusy, setResetBusy] = useState(false);

  // Gmail Actions State
  const [gmailBusy, setGmailBusy] = useState(false);
  const [gmailSyncResult, setGmailSyncResult] = useState<GmailSyncResult | null>(null);
  const [showLogs, setShowLogs] = useState(false);

  const dirtyRef = useRef(false);

  async function loadState() {
    try {
      const [sRes, setRes, autoRes, portRes, riskRes] = await Promise.allSettled([
        apiFetch<SetupStatus>("/setup/status"),
        apiFetch<IntegrationSettings>("/integrations/settings"),
        apiFetch<AutomationStatus>("/automation/status"),
        apiFetch<PortfolioOverview>("/portfolio/overview"),
        apiFetch<RiskSummary>("/risk/summary?refresh=false"),
      ]);

      if (setRes.status === "fulfilled") {
        setSettings(prev => (prev && dirtyRef.current ? prev : setRes.value));
      }
      if (sRes.status === "fulfilled") setSetup(sRes.value);
      if (autoRes.status === "fulfilled") setAutomation(autoRes.value);
      if (portRes.status === "fulfilled") setPortfolioOverview(portRes.value);
      if (riskRes.status === "fulfilled") setRiskSummary(riskRes.value);

      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load settings.");
    }
  }

  useEffect(() => {
    void loadState();
    const interval = window.setInterval(loadState, 20000);
    return () => window.clearInterval(interval);
  }, []);

  function markDirty() {
    dirtyRef.current = true;
    setIsDirty(true);
  }

  function updateSettings(updater: (current: IntegrationSettings) => IntegrationSettings) {
    markDirty();
    setSettings(current => current ? updater(current) : current);
  }

  async function handleSave() {
    if (!settings) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`${API_BASE}/integrations/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...settings,
          gmail: { ...settings.gmail, password: gmailPassword || undefined },
          plaid: {
            ...settings.plaid,
            client_id: plaidClientId || undefined,
            secret: plaidSecret || undefined,
          },
          llm: { ...settings.llm, api_key: llmApiKey || undefined },
          research: { ...settings.research, api_key: researchApiKey || undefined },
        }),
      });
      if (!response.ok) throw new Error(await response.text());

      setSettings(await response.json());
      dirtyRef.current = false;
      setIsDirty(false);
      setGmailPassword("");
      setLlmApiKey("");
      setResearchApiKey("");
      setPlaidClientId("");
      setPlaidSecret("");
      setNotice("Settings saved successfully.");
      await loadState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save settings.");
    } finally {
      setSaving(false);
    }
  }

  function handleDiscard() {
    dirtyRef.current = false;
    setIsDirty(false);
    setGmailPassword("");
    setLlmApiKey("");
    setResearchApiKey("");
    setPlaidClientId("");
    setPlaidSecret("");
    void loadState();
  }

  async function handleGmailTest() {
    if (!settings) return;
    setGmailBusy(true);
    setError(null);
    try {
      const res = await apiFetch<GmailTestResult>("/integrations/gmail/test", {
        method: "POST",
        body: JSON.stringify({
          ...settings.gmail,
          password: gmailPassword || undefined,
        }),
      });
      setNotice(`Connection test successful. Matched ${res.matched_messages} sample messages.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Gmail test failed.");
    } finally {
      setGmailBusy(false);
    }
  }

  async function handleGmailSync() {
    setGmailBusy(true);
    setError(null);
    try {
      const res = await apiFetch<GmailSyncResult>("/integrations/gmail/sync", { method: "POST" });
      setGmailSyncResult(res);
      setNotice(`Sync complete. Created ${res.transactions_created ?? 0} transactions.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Gmail sync failed.");
    } finally {
      setGmailBusy(false);
    }
  }

  async function handleGmailBackfill() {
    if (!window.confirm("Perform a deep backfill of the scoped Gmail label? This may take a minute.")) return;
    setGmailBusy(true);
    setError(null);
    try {
      const res = await apiFetch<GmailSyncResult>("/integrations/gmail/backfill", { method: "POST" });
      setGmailSyncResult(res);
      setNotice(`Backfill started in background. Discoveries will appear in your portfolio automatically.`);
      setShowLogs(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Gmail backfill failed.");
    } finally {
      setGmailBusy(false);
    }
  }

  async function handleHardReset() {
    if (resetPhrase.trim().toUpperCase() !== "RESET INVESTOS") {
      setError("Type RESET INVESTOS to confirm.");
      return;
    }
    if (!window.confirm("Permanently delete all portfolio history and reset Prophet?")) return;

    setResetBusy(true);
    try {
      const res = await fetch(`${API_BASE}/setup/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmation_text: resetPhrase }),
      });
      if (!res.ok) throw new Error(await res.text());
      setNotice("System reset complete.");
      setResetPhrase("");
      await loadState();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Reset failed.");
    } finally {
      setResetBusy(false);
    }
  }

  if (!settings) {
    return <div className="p-12 animate-pulse text-gray-500">Loading your configuration...</div>;
  }
  const activeLlmCapability = (settings.llm.available_providers ?? []).find(
    capability => capability.provider === settings.llm.provider,
  );

  return (
    <div className="min-h-screen bg-gray-50/60 text-gray-900 dark:bg-[#0a0a0a] dark:text-gray-100">
      <AppNav active="settings" />

      <main className="mx-auto w-full max-w-[1440px] px-4 py-8 sm:px-6 lg:px-8">
        {error && <FloatingNotice tone="error" message={error} onDismiss={() => setError(null)} />}
        {notice && <FloatingNotice tone="success" message={notice} onDismiss={() => setNotice(null)} />}

        <div className="mb-8 flex flex-col justify-between gap-5 border-b border-gray-200 pb-5 md:flex-row md:items-end dark:border-gray-800">
          <div>
            <h1 className="text-3xl font-semibold">Settings</h1>
            <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
              Manage your data connections, research providers, and system state.
            </p>
          </div>

          <div className="flex w-full items-start gap-2 md:w-auto">
            <div className="grid min-w-0 flex-1 grid-cols-2 border-b border-gray-200 sm:flex dark:border-gray-800">
              <TabButton active={activeTab === "overview"} onClick={() => setActiveTab("overview")} label="Overview" icon={<CheckCircle2 className="w-4 h-4" />} />
              <TabButton active={activeTab === "data"} onClick={() => setActiveTab("data")} label="Data Connections" icon={<Database className="w-4 h-4" />} />
              <TabButton active={activeTab === "research"} onClick={() => setActiveTab("research")} label="Research" icon={<Search className="w-4 h-4" />} />
              <TabButton active={activeTab === "system"} onClick={() => setActiveTab("system")} label="System" icon={<Shield className="w-4 h-4" />} />
            </div>
            <button
              onClick={() => setShowLogs(!showLogs)}
              className={`mt-0.5 shrink-0 rounded p-2 transition-colors ${showLogs ? "bg-gray-100 text-gray-900 dark:bg-gray-800 dark:text-gray-100" : "text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"}`}
              title="Show Live Logs"
              aria-label="Show live logs"
            >
              <Terminal className="w-4 h-4" />
            </button>
          </div>
        </div>

        <div className="space-y-6 animate-in fade-in duration-300">
          {activeTab === "overview" && (
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.72fr)]">
              <section className="space-y-5">
                <Card title="System Readiness" icon={<Zap className="h-5 w-5 text-gray-500" />}>
                  <div className="space-y-4">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium">Configuration Progress</span>
                      <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">{Math.round((setup?.completion_ratio ?? 0) * 100)}%</span>
                    </div>
                    <div className="h-2 w-full bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                      <div className="h-full bg-gray-900 transition-all duration-1000 dark:bg-gray-100" style={{ width: `${(setup?.completion_ratio ?? 0) * 100}%` }} />
                    </div>
                    <p className="text-xs text-gray-500">
                      Next step: <span className="font-bold text-gray-700 dark:text-gray-300">{setup?.next_recommended_step ?? "All core setup steps are complete"}</span>
                    </p>
                    <p className="text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                      Readiness covers data, credentials, safety gates, and automation prerequisites. Local evidence can still work while a live provider is incomplete.
                    </p>
                  </div>
                </Card>

                <div className="divide-y divide-gray-200 overflow-hidden rounded-lg border border-gray-200 bg-white dark:divide-gray-800 dark:border-gray-800 dark:bg-gray-950">
                  {setup?.steps.map(step => {
                    const tone = setupStepTone(step.status);
                    const targetTab = setupStepTargetTab(step.id);
                    return (
                      <div key={step.id} className="p-4">
                        <div className="flex items-start justify-between gap-4">
                          <div className="flex items-start gap-3">
                            {step.status === "complete" ? (
                              <CheckCircle2 className={`mt-0.5 h-5 w-5 ${tone.icon}`} />
                            ) : (
                              <AlertCircle className={`mt-0.5 h-5 w-5 ${tone.icon}`} />
                            )}
                            <div className="min-w-0">
                              <p className="text-sm font-bold">{step.label}</p>
                              <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">{step.description}</p>
                            </div>
                          </div>
                          <span className={`shrink-0 rounded px-2 py-1 text-xs font-medium ${tone.badge}`} title={`Raw status: ${step.status}`}>
                            {step.status_label ?? formatStatusText(step.status)}
                          </span>
                        </div>
                        {step.detail ? (
                          <p className="mt-3 border-l-2 border-gray-200 pl-3 text-xs text-gray-600 dark:border-gray-700 dark:text-gray-300">
                            {step.detail}
                          </p>
                        ) : null}
                        {step.hint ? (
                          <p className="mt-2 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                            {step.hint}
                          </p>
                        ) : null}
                        {targetTab ? (
                          <button
                            type="button"
                            onClick={() => setActiveTab(targetTab)}
                            className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-gray-700 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white"
                          >
                            {step.action_label ?? "Open settings"}
                            <ArrowRight className="h-3.5 w-3.5" />
                          </button>
                        ) : step.href ? (
                          <a
                            href={step.href}
                            className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-gray-700 hover:text-gray-950 dark:text-gray-300 dark:hover:text-white"
                          >
                            {step.action_label ?? "Open"}
                            <ArrowRight className="h-3.5 w-3.5" />
                          </a>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </section>

              <aside className="space-y-6">
                <Card title="Live Status" icon={<Shield className="h-5 w-5 text-gray-500" />}>
                  <div className="grid grid-cols-2 gap-4">
                    <StatItem label="Holdings" value={String(portfolioOverview?.holdings.length ?? 0)} />
                    <StatItem label="Market Value" value={safeFormatCurrency((portfolioOverview?.holdings ?? []).reduce((s, h) => s + h.market_value, 0))} />
                    <StatItem label="Benchmark" value={riskSummary?.active_benchmark?.ticker || "SPY"} />
                    <StatItem label="Regime" value={formatRegimeLabel(riskSummary?.current_regime?.regime_type)} />
                  </div>
                </Card>

                <Card title="Automation Status">
                  <div className="divide-y divide-gray-200 dark:divide-gray-800">
                    {automation?.jobs.map(job => {
                      const statusView = automationStatusView(job);
                      return (
                      <div key={job.name} className="py-3 first:pt-0 last:pb-0">
                        <div className="flex items-start justify-between gap-4">
                          <div className="min-w-0">
                            <span className="text-xs font-bold text-gray-900 dark:text-gray-100">{formatJobName(job.name)}</span>
                            <p className="mt-1 text-[11px] leading-relaxed text-gray-500 dark:text-gray-400">
                              {automationJobDescription(job.name)} {statusView.meaning}
                            </p>
                          </div>
                          <span className={`shrink-0 text-xs font-medium ${statusView.text}`} title={`Raw status: ${job.last_status}`}>
                            {formatStatusText(job.last_status)}
                          </span>
                        </div>
                        {job.detail ? (
                          <p className="mt-2 border-l-2 border-gray-200 pl-2 text-[11px] text-gray-600 dark:border-gray-700 dark:text-gray-300">
                            {humanizeJobDetail(job.detail)}
                          </p>
                        ) : null}
                        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400">
                          <span className="inline-flex items-center gap-1">
                            <Clock className="h-3 w-3" />
                            {formatInterval(job.interval_seconds)}
                          </span>
                          <span>{formatJobTime(job.last_run_at)}</span>
                        </div>
                      </div>
                      );
                    }) ?? <p className="text-sm text-gray-400">No active jobs.</p>}
                  </div>
                </Card>
              </aside>
            </div>
          )}

          {activeTab === "data" && (
            <div className="max-w-4xl space-y-6">
              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                    <Database className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold">Gmail Data Ingestion</h2>
                    <p className="text-sm text-gray-500">Prophet securely parses broker confirmations from a scoped label.</p>
                  </div>
                </div>

                <div className="mb-8 flex items-center justify-between border-y border-slate-200 py-3 dark:border-slate-800">
                  <div className="flex items-center gap-3">
                    <div className={`h-2.5 w-2.5 rounded-full ${settings.gmail.enabled ? "bg-emerald-500" : "bg-slate-400"}`} />
                    <span className="text-sm font-medium text-slate-600 dark:text-slate-300">Service status: {settings.gmail.enabled ? "Active" : "Disabled"}</span>
                  </div>
                  <button
                    onClick={() => updateSettings(c => ({ ...c, gmail: { ...c.gmail, enabled: !c.gmail.enabled }}))}
                    className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                      settings.gmail.enabled
                        ? "bg-red-50 text-red-600 hover:bg-red-100"
                        : "bg-emerald-50 text-emerald-600 hover:bg-emerald-100"
                    }`}
                  >
                    {settings.gmail.enabled ? "Disable Sync" : "Enable Sync"}
                  </button>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
                  <Field label="Gmail Username">
                    <input
                      value={settings.gmail.username}
                      onChange={e => updateSettings(c => ({ ...c, gmail: { ...c.gmail, username: e.target.value }}))}
                      className={inputClass}
                      placeholder="you@gmail.com"
                    />
                  </Field>
                  <Field label={`App Password ${settings.gmail.password_set ? "(set)" : ""}`}>
                    <input
                      type="password"
                      value={gmailPassword}
                      onChange={e => {
                        markDirty();
                        setGmailPassword(e.target.value);
                      }}
                      className={inputClass}
                      placeholder={settings.gmail.password_set ? "Leave blank to keep current" : "Enter Google app password"}
                    />
                    <SecretStatus saved={settings.gmail.password_set} pending={Boolean(gmailPassword.trim())} />
                  </Field>
                  <Field label="Scoped Folder (Label)">
                    <input
                      value={settings.gmail.folder}
                      onChange={e => updateSettings(c => ({ ...c, gmail: { ...c.gmail, folder: e.target.value }}))}
                      className={inputClass}
                    />
                  </Field>
                </div>

                <div className="flex flex-wrap gap-4 pt-6 border-t border-slate-100 dark:border-slate-800">
                  <button
                    onClick={handleGmailTest}
                    disabled={gmailBusy}
                    className="rounded-md border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium transition-colors hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:hover:bg-slate-800"
                  >
                    Test Connection
                  </button>
                  <button
                    onClick={handleGmailSync}
                    disabled={gmailBusy || !settings.gmail.enabled}
                    className="rounded-md bg-gray-900 px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950 dark:hover:bg-gray-300"
                  >
                    {gmailBusy ? "Syncing..." : "Sync Recent"}
                  </button>
                  <button
                    onClick={handleGmailBackfill}
                    disabled={gmailBusy}
                    className="rounded-md border border-slate-300 px-4 py-2.5 text-sm font-medium transition-colors hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:hover:bg-slate-900"
                  >
                    Deep Backfill
                  </button>
                </div>
                {gmailSyncResult ? (
                  <div className="mt-5 rounded-md border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700 dark:border-slate-800 dark:bg-slate-900/50 dark:text-slate-300">
                    <div className="font-semibold">Latest Gmail operation</div>
                    <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                      {gmailSyncResult.status ? `${gmailSyncResult.status} · ` : ""}
                      processed {gmailSyncResult.processed_messages ?? 0} messages · created{" "}
                      {gmailSyncResult.transactions_created ?? 0} transactions
                    </div>
                    {gmailSyncResult.detail ? (
                      <div className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                        {gmailSyncResult.detail}
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </section>

              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="mb-6 flex items-start justify-between gap-4">
                  <div className="flex items-center gap-3">
                    <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                      <Landmark className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                    </div>
                    <div>
                      <h2 className="text-xl font-semibold">Paper Broker</h2>
                      <p className="text-sm text-gray-500">Deterministic orders and fills for Shadow Lab. This broker cannot route real trades.</p>
                    </div>
                  </div>
                  <label className="flex items-center gap-2 text-sm font-medium">
                    <input
                      type="checkbox"
                      checked={settings.paper_trading.enabled}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, enabled: event.target.checked },
                      }))}
                      className="h-4 w-4"
                    />
                    Enabled
                  </label>
                </div>

                <p className="mb-5 text-sm text-gray-500">{settings.paper_trading.status_message}</p>
                <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
                  <Field label="Execution Provider">
                    <select
                      value={settings.paper_trading.provider}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, provider: event.target.value },
                      }))}
                      className={inputClass}
                    >
                      <option value="local_simulator">Local deterministic simulator</option>
                    </select>
                  </Field>
                  <Field label="Slippage (bps)">
                    <input
                      type="number"
                      min="0"
                      max="1000"
                      step="0.1"
                      value={settings.paper_trading.slippage_bps}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, slippage_bps: Number(event.target.value) || 0 },
                      }))}
                      className={inputClass}
                    />
                  </Field>
                  <Field label="Fee per Order ($)">
                    <input
                      type="number"
                      min="0"
                      step="0.01"
                      value={settings.paper_trading.fee_per_order}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, fee_per_order: Number(event.target.value) || 0 },
                      }))}
                      className={inputClass}
                    />
                  </Field>
                  <Field label="Maximum Buy Order (% Equity)">
                    <input
                      type="number"
                      min="0.1"
                      max="100"
                      step="0.1"
                      value={settings.paper_trading.max_buy_order_pct_equity}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, max_buy_order_pct_equity: Number(event.target.value) || 0.1 },
                      }))}
                      className={inputClass}
                    />
                  </Field>
                  <label className="flex items-center gap-3 rounded-md border border-gray-200 px-4 py-3 text-sm dark:border-gray-800">
                    <input
                      type="checkbox"
                      checked={settings.paper_trading.allow_fractional}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, allow_fractional: event.target.checked },
                      }))}
                      className="h-4 w-4"
                    />
                    Allow fractional shares
                  </label>
                  <label className="flex items-center gap-3 rounded-md border border-gray-200 px-4 py-3 text-sm dark:border-gray-800">
                    <input
                      type="checkbox"
                      checked={settings.paper_trading.require_regular_session}
                      onChange={event => updateSettings(current => ({
                        ...current,
                        paper_trading: { ...current.paper_trading, require_regular_session: event.target.checked },
                      }))}
                      className="h-4 w-4"
                    />
                    Fill only in regular session
                  </label>
                </div>
              </section>

              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="mb-6 flex items-start justify-between gap-4">
                  <div className="flex items-center gap-3">
                    <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                      <Landmark className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                    </div>
                    <div>
                      <h2 className="text-xl font-semibold">Brokerage Reconciliation</h2>
                      <p className="text-sm text-gray-500">Connect through Plaid Investments and compare broker holdings with Prophet&apos;s evidence-built ledger.</p>
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => updateSettings(c => ({ ...c, plaid: { ...c.plaid, enabled: !c.plaid.enabled } }))}
                    className={`rounded-md px-3 py-1.5 text-xs font-medium ${settings.plaid.enabled ? "bg-red-50 text-red-700 dark:bg-red-950/30 dark:text-red-300" : "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300"}`}
                  >
                    {settings.plaid.enabled ? "Disable" : "Enable"}
                  </button>
                </div>

                <div className="mb-5 border-l-2 border-gray-200 pl-3 text-sm text-gray-600 dark:border-gray-700 dark:text-gray-300">
                  <p>{settings.plaid.status_message ?? "Brokerage status unavailable."}</p>
                  <p className="mt-1 text-xs text-gray-500">
                    Reconciliation never silently overwrites lots or cost basis. Differences become review items so broker truth and transaction evidence stay auditable.
                  </p>
                </div>

                <div className="grid gap-5 md:grid-cols-3">
                  <Field label="Plaid Environment">
                    <select
                      value={settings.plaid.environment}
                      onChange={event => updateSettings(c => ({ ...c, plaid: { ...c.plaid, environment: event.target.value } }))}
                      className={inputClass}
                    >
                      <option value="sandbox">Sandbox</option>
                      <option value="development">Development</option>
                      <option value="production">Production</option>
                    </select>
                  </Field>
                  <Field label={`Client ID ${settings.plaid.client_id_set ? "(set)" : ""}`}>
                    <input
                      type="password"
                      value={plaidClientId}
                      onChange={event => {
                        markDirty();
                        setPlaidClientId(event.target.value);
                      }}
                      className={inputClass}
                      placeholder={settings.plaid.client_id_set ? "Leave blank to keep current" : "Plaid client ID"}
                    />
                    <SecretStatus saved={settings.plaid.client_id_set} pending={Boolean(plaidClientId.trim())} />
                  </Field>
                  <Field label={`Secret ${settings.plaid.secret_set ? "(set)" : ""}`}>
                    <input
                      type="password"
                      value={plaidSecret}
                      onChange={event => {
                        markDirty();
                        setPlaidSecret(event.target.value);
                      }}
                      className={inputClass}
                      placeholder={settings.plaid.secret_set ? "Leave blank to keep current" : "Plaid secret"}
                    />
                    <SecretStatus saved={settings.plaid.secret_set} pending={Boolean(plaidSecret.trim())} />
                  </Field>
                </div>

                <div className="mt-6 border-t border-gray-200 pt-5 dark:border-gray-800">
                  <PlaidConnectionControl
                    enabled={settings.plaid.enabled}
                    credentialsReady={settings.plaid.client_id_set && settings.plaid.secret_set}
                    connected={settings.plaid.access_token_set}
                    onError={setError}
                    onNotice={setNotice}
                    onChanged={loadState}
                  />
                </div>
              </section>

              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                    <Shield className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold">Portfolio Baseline</h2>
                    <p className="text-sm text-gray-500">Define your benchmark and available buying power.</p>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <Field label="Benchmark Ticker">
                    <input
                      value={settings.portfolio.default_benchmark_ticker}
                      onChange={e => updateSettings(c => ({ ...c, portfolio: { ...c.portfolio, default_benchmark_ticker: e.target.value.toUpperCase() }}))}
                      className={inputClass}
                    />
                  </Field>
                  <Field label="Remaining Buying Power ($)">
                    <input
                      type="number"
                      value={settings.portfolio.remaining_buying_power}
                      onChange={e => updateSettings(c => ({ ...c, portfolio: { ...c.portfolio, remaining_buying_power: Number(e.target.value) || 0 }}))}
                      className={inputClass}
                    />
                  </Field>
                </div>
              </section>

              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                    <Zap className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold">Market Data</h2>
                    <p className="text-sm text-gray-500">Configure how Prophet retrieves live pricing and ticker info.</p>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <Field label="Data Provider">
                    <select
                      value={settings.market_data.provider}
                      onChange={e => updateSettings(c => ({ ...c, market_data: { ...c.market_data, provider: e.target.value }}))}
                      className={inputClass}
                    >
                      <option value="yahoo_finance">Yahoo Finance</option>
                      <option value="polygon">Polygon (API Key Req.)</option>
                    </select>
                  </Field>
                  <Field label="Refresh Interval (Seconds)">
                    <input
                      type="number"
                      value={settings.market_data.refresh_interval_seconds}
                      onChange={e => updateSettings(c => ({ ...c, market_data: { ...c.market_data, refresh_interval_seconds: Number(e.target.value) || 60 }}))}
                      className={inputClass}
                    />
                  </Field>
                </div>
              </section>
            </div>
          )}

          {activeTab === "research" && (
            <div className="max-w-4xl space-y-6">
              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                    <Search className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold">Research Connectivity</h2>
                    <p className="text-sm text-gray-500">Configure how Prophet searches the broader web.</p>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                  <Field label="Research Search Provider">
                    <select
                      value={settings.research.provider}
                      onChange={e => updateSettings(c => ({ ...c, research: { ...c.research, provider: e.target.value }}))}
                      className={inputClass}
                    >
                      <option value="tavily">Tavily</option>
                    </select>
                  </Field>
                  <Field label={`Tavily API Key ${settings.research.api_key_set ? "(set)" : ""}`}>
                    <input
                      type="password"
                      value={researchApiKey}
                      onChange={e => {
                        markDirty();
                        setResearchApiKey(e.target.value);
                      }}
                      className={inputClass}
                      placeholder={settings.research.api_key_set ? "Leave blank to keep current" : "Paste API key"}
                    />
                    <SecretStatus saved={settings.research.api_key_set} pending={Boolean(researchApiKey.trim())} />
                  </Field>
                </div>
              </section>

              <section className="rounded-lg border border-gray-200 bg-white p-6 dark:border-gray-800 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-gray-200 p-2 dark:border-gray-800">
                    <Zap className="h-6 w-6 text-gray-600 dark:text-gray-300" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold">Extraction & Intelligence</h2>
                    <p className="text-sm text-gray-500">The LLM brain used for data extraction and research analysis.</p>
                  </div>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
                  <Field label="Intelligence Provider">
                    <select
                      value={settings.llm.provider}
                      onChange={e => {
                        const p = e.target.value;
                        updateSettings(c => {
                          const capability = (c.llm.available_providers ?? []).find(
                            item => item.provider === p,
                          );
                          return {
                            ...c,
                            llm: {
                              ...c.llm,
                              provider: p,
                              hosted_model: capability?.default_model ?? "",
                              hosted_base_url: capability?.default_base_url ?? "",
                            },
                          };
                        });
                      }}
                      className={inputClass}
                    >
                      {(settings.llm.available_providers ?? []).map(capability => (
                        <option key={capability.provider} value={capability.provider}>
                          {capability.label}
                        </option>
                      ))}
                    </select>
                  </Field>

                  {activeLlmCapability?.requires_api_key && (
                    <Field label={`API Key ${settings.llm.api_key_set ? "(set)" : ""}`}>
                      <input
                        type="password"
                        value={llmApiKey}
                        onChange={e => {
                          markDirty();
                          setLlmApiKey(e.target.value);
                        }}
                        className={inputClass}
                        placeholder={settings.llm.api_key_set ? "Leave blank to keep current" : "Paste provider API key"}
                      />
                      <SecretStatus saved={settings.llm.api_key_set} pending={Boolean(llmApiKey.trim())} />
                    </Field>
                  )}

                  {activeLlmCapability?.accepts_model && (
                    <Field label={activeLlmCapability.is_local ? "Local Model Tag" : "Model Identifier"}>
                      <input
                        value={settings.llm.hosted_model}
                        onChange={e => updateSettings(c => ({ ...c, llm: { ...c.llm, hosted_model: e.target.value }}))}
                        className={inputClass}
                        placeholder={activeLlmCapability.default_model || "Provider model identifier"}
                      />
                    </Field>
                  )}

                  {activeLlmCapability?.accepts_base_url && (
                    <Field label={activeLlmCapability.is_local ? "Local Provider Endpoint" : "Base URL"}>
                      <input
                        value={settings.llm.hosted_base_url}
                        onChange={e => updateSettings(c => ({ ...c, llm: { ...c.llm, hosted_base_url: e.target.value }}))}
                        className={inputClass}
                        placeholder={activeLlmCapability.default_base_url || "Provider endpoint"}
                      />
                    </Field>
                  )}
                </div>
              </section>
            </div>
          )}

          {activeTab === "system" && (
            <div className="max-w-4xl space-y-6">
              <section className="rounded-lg border border-red-200 bg-white p-6 dark:border-red-900/40 dark:bg-gray-950">
                <div className="flex items-center gap-3 mb-8">
                  <div className="rounded-md border border-red-200 p-2 dark:border-red-900/40">
                    <Trash2 className="w-6 h-6 text-red-600" />
                  </div>
                  <div>
                    <h2 className="text-xl font-semibold text-red-600">Danger Zone</h2>
                    <p className="text-sm text-gray-500">Permanent actions to reset your environment.</p>
                  </div>
                </div>

                <div className="space-y-6">
                  {setup?.development_reset_enabled ? (
                    <div className="border-t border-red-100 pt-6 dark:border-red-900/30">
                      <h3 className="font-bold text-red-700 dark:text-red-400">Hard System Reset</h3>
                      <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
                        This will delete all transaction history, research profiles, and knowledge nodes. This action cannot be undone.
                      </p>

                      <div className="mt-6 space-y-4">
                        <p className="text-xs font-bold uppercase tracking-wider text-gray-400">Type RESET INVESTOS to confirm</p>
                        <input
                          value={resetPhrase}
                          onChange={e => setResetPhrase(e.target.value)}
                          className={`${inputClass} border-red-200 focus:ring-red-500`}
                          placeholder="RESET INVESTOS"
                        />
                        <button
                          onClick={handleHardReset}
                          disabled={resetBusy || resetPhrase !== "RESET INVESTOS"}
                          className="w-full rounded-md bg-red-600 py-3 text-white font-bold hover:bg-red-500 disabled:opacity-50 transition-all"
                        >
                          {resetBusy ? "Resetting..." : "Confirm Hard Reset"}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="flex items-start gap-3 border-t border-gray-200 pt-5 dark:border-gray-800">
                      <Shield className="mt-0.5 h-5 w-5 shrink-0 text-gray-500" />
                      <div>
                        <h3 className="font-semibold text-gray-900 dark:text-gray-100">Development reset disabled</h3>
                        <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
                          Destructive reset is unavailable unless it is explicitly enabled for a local development session.
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              </section>
            </div>
          )}
        </div>
      </main>

      <FloatingSaveBar
        isDirty={isDirty}
        saving={saving}
        onSave={handleSave}
        onDiscard={handleDiscard}
      />

      {showLogs && (
        <LiveLogConsole onClose={() => setShowLogs(false)} />
      )}
    </div>
  );
}

function PlaidConnectionControl({
  enabled,
  credentialsReady,
  connected,
  onError,
  onNotice,
  onChanged,
}: {
  enabled: boolean;
  credentialsReady: boolean;
  connected: boolean;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
  onChanged: () => Promise<void>;
}) {
  const [linkToken, setLinkToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!enabled || !credentialsReady || connected) {
      setLinkToken(null);
      return;
    }
    let cancelled = false;
    void apiFetch<{ link_token: string }>("/integrations/plaid/link-token", { method: "POST" })
      .then(result => {
        if (!cancelled) setLinkToken(result.link_token);
      })
      .catch(err => {
        if (!cancelled) onError(err instanceof Error ? err.message : "Unable to initialize Plaid Link.");
      });
    return () => {
      cancelled = true;
    };
  }, [connected, credentialsReady, enabled, onError]);

  async function reconcile() {
    setBusy(true);
    onError(null);
    try {
      const result = await apiFetch<{ differences: unknown[]; review_items_created: number }>(
        "/integrations/plaid/reconcile",
        { method: "POST" },
      );
      onNotice(
        `Brokerage reconciliation complete: ${result.differences.length} difference(s), ${result.review_items_created} new review item(s).`,
      );
      await onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Brokerage reconciliation failed.");
    } finally {
      setBusy(false);
    }
  }

  if (!enabled) {
    return <p className="text-sm text-gray-500">Enable broker sync, save, then connect an account.</p>;
  }
  if (!credentialsReady) {
    return <p className="text-sm text-amber-700 dark:text-amber-300">Save a Plaid client ID and secret before opening Plaid Link.</p>;
  }
  return (
    <div className="flex flex-wrap items-center gap-3">
      {!connected ? (
        <PlaidLinkLauncher
          linkToken={linkToken}
          busy={busy}
          onBusyChange={setBusy}
          onError={onError}
          onNotice={onNotice}
          onChanged={onChanged}
          onReconcile={reconcile}
        />
      ) : (
        <button
          type="button"
          onClick={() => void reconcile()}
          disabled={busy}
          className="rounded-md bg-gray-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950"
        >
          {busy ? "Reconciling..." : "Reconcile now"}
        </button>
      )}
      <span className="text-xs text-gray-500">
        {connected ? "Connected; an automatic check runs every 6 hours." : "Plaid Link opens in a secure provider flow."}
      </span>
    </div>
  );
}

function PlaidLinkLauncher({
  linkToken,
  busy,
  onBusyChange,
  onError,
  onNotice,
  onChanged,
  onReconcile,
}: {
  linkToken: string | null;
  busy: boolean;
  onBusyChange: (busy: boolean) => void;
  onError: (message: string | null) => void;
  onNotice: (message: string | null) => void;
  onChanged: () => Promise<void>;
  onReconcile: () => Promise<void>;
}) {
  const { open, ready, error } = usePlaidLink({
    token: linkToken,
    onSuccess: publicToken => {
      onBusyChange(true);
      onError(null);
      void apiFetch<{ ok: boolean }>("/integrations/plaid/exchange", {
        method: "POST",
        body: JSON.stringify({ public_token: publicToken }),
      })
        .then(async () => {
          onNotice("Brokerage connected. Running the first holdings reconciliation now.");
          await onChanged();
          await onReconcile();
        })
        .catch(err => onError(err instanceof Error ? err.message : "Plaid token exchange failed."))
        .finally(() => onBusyChange(false));
    },
    onExit: plaidError => {
      if (plaidError) onError(plaidError.display_message ?? plaidError.error_message ?? "Plaid Link closed with an error.");
    },
  });

  return (
    <button
      type="button"
      onClick={() => open()}
      disabled={!ready || busy || !linkToken || Boolean(error)}
      className="rounded-md bg-gray-900 px-4 py-2.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-950"
      title={error ? "Plaid Link is unavailable right now." : undefined}
    >
      {busy ? "Connecting..." : error ? "Plaid unavailable" : "Connect brokerage"}
    </button>
  );
}

type SetupTone = {
  border: string;
  badge: string;
  icon: string;
};

function setupStepTone(status: string): SetupTone {
  if (status === "complete") {
    return {
      border: "border-emerald-100 dark:border-emerald-900/40",
      badge: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300",
      icon: "text-emerald-500",
    };
  }
  if (status === "in_progress") {
    return {
      border: "border-amber-100 dark:border-amber-900/40",
      badge: "bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300",
      icon: "text-amber-500",
    };
  }
  return {
    border: "border-gray-200 dark:border-gray-800",
    badge: "bg-gray-100 text-gray-600 dark:bg-gray-900 dark:text-gray-300",
    icon: "text-gray-400",
  };
}

function setupStepTargetTab(stepId: string): SettingsTab | null {
  if (["gmail_scope", "brokerage_sync", "live_prices"].includes(stepId)) return "data";
  if (["llm_provider", "research_provider", "research_memory"].includes(stepId)) return "research";
  return null;
}

function formatStatusText(value?: string | null): string {
  if (!value) return "Unknown";
  return value.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

const AUTOMATION_JOB_COPY: Record<string, { label: string; description: string }> = {
  database_backup: { label: "Database backup", description: "Creates a recoverable database snapshot." },
  integrity_audit: { label: "Integrity and graph repair", description: "Validates stored rows, preserves unknown graph types for review, and restores missing metric or market-signal links from canonical records." },
  research_loop: { label: "Research queue", description: "Advances the highest-priority unresolved research question." },
  question_resolution: { label: "Question resolution", description: "Tests whether new evidence directly resolves an open investor question." },
  relation_review: { label: "Relationship review", description: "Reviews candidate links between holdings, entities, themes, and evidence." },
  agent_reflection: { label: "Portfolio review", description: "Looks across current evidence for blind spots, contradictions, and useful next actions." },
  shadow_refresh: { label: "Paper portfolio refresh", description: "Marks and advances active shadow-investment checkpoints without touching real positions." },
  evidence_processing: { label: "Evidence processing", description: "Extracts pending source material into dated, attributable research records." },
  strategist_cycle: { label: "Strategy review", description: "Reassesses portfolio-level risks, opportunities, and research priorities." },
  shadow_discovery: { label: "Paper idea discovery", description: "Looks for evidence-backed ideas worth testing in the shadow portfolio." },
  pattern_discovery: { label: "Pattern and cycle review", description: "Looks across independently sourced signals, portfolio relationships, the current regime, and historical rhymes to create provisional hypotheses with falsifiers and next checks." },
  watcher_loop: { label: "Catalyst monitors", description: "Evaluates active event, price, filing, and thesis-change monitors." },
  entity_hygiene: { label: "Entity cleanup", description: "Merges duplicates and removes or reclassifies non-investable artifact entities." },
  theme_hygiene: { label: "Theme cleanup", description: "Consolidates duplicate or placeholder research themes while preserving useful evidence." },
  media_cleanup: { label: "Media cleanup", description: "Removes expired temporary media files while preserving extracted evidence and provenance." },
  source_claim_assessment: { label: "Source calibration", description: "Tests due source claims against later evidence, defers inconclusive reviews with a visible retry time, and can launch one bounded follow-up research pass." },
  market_setup_assessment: { label: "Setup outcome review", description: "Revisits due expectations against later evidence, defers uncertain cases with retry timing, and can launch bounded follow-up research." },
  fundamental_freshness: { label: "Metric freshness", description: "Marks financial metrics current or stale based on their dates and reporting cadence." },
  investment_object_backfill: { label: "Historical research reindex", description: "Gradually converts older evidence into dated metrics and market-setup signals." },
  market_data_refresh: { label: "Market data", description: "Refreshes security prices used by positions, risk, and watcher calculations." },
  risk_refresh: { label: "Risk model", description: "Recomputes concentration, correlation, benchmark, and scenario risk." },
  gmail_sync: { label: "Inbox transaction sync", description: "Reads only the configured Gmail scope and imports supported broker confirmations." },
  brokerage_reconcile: { label: "Brokerage reconciliation", description: "Compares linked brokerage holdings with Prophet and queues material differences for review." },
};

function formatJobName(name: string): string {
  return AUTOMATION_JOB_COPY[name]?.label ?? formatStatusText(name);
}

function automationJobDescription(name: string): string {
  return AUTOMATION_JOB_COPY[name]?.description ?? "Runs a scheduled Prophet maintenance or research workflow.";
}

type AutomationJob = AutomationStatus["jobs"][number];

function automationStatusView(job: AutomationJob) {
  if (!job.enabled || job.last_status === "disabled") {
    return {
      bg: "bg-gray-50 dark:bg-gray-900",
      border: "border-gray-100 dark:border-gray-800",
      text: "text-gray-500 dark:text-gray-400",
      meaning: "Disabled by runtime settings.",
    };
  }
  if (job.last_status === "error") {
    return {
      bg: "bg-red-50/70 dark:bg-red-950/20",
      border: "border-red-100 dark:border-red-900/40",
      text: "text-red-600 dark:text-red-300",
      meaning: "Last run failed; open logs or rerun after fixing the cause.",
    };
  }
  if (job.last_status === "warning") {
    return {
      bg: "bg-amber-50/70 dark:bg-amber-950/20",
      border: "border-amber-100 dark:border-amber-900/40",
      text: "text-amber-700 dark:text-amber-300",
      meaning: "Last run completed with a warning that may need review.",
    };
  }
  if (job.last_status === "waiting_for_config") {
    return {
      bg: "bg-amber-50/70 dark:bg-amber-950/20",
      border: "border-amber-100 dark:border-amber-900/40",
      text: "text-amber-700 dark:text-amber-300",
      meaning: "Enabled, but waiting for a required setting before it can run.",
    };
  }
  if (job.last_status === "idle") {
    return {
      bg: "bg-gray-50 dark:bg-gray-900",
      border: "border-gray-100 dark:border-gray-800",
      text: "text-gray-500 dark:text-gray-400",
      meaning: "Enabled and waiting for the next scheduled run.",
    };
  }
  if (job.last_status === "cancelled") {
    return {
      bg: "bg-slate-50 dark:bg-slate-900",
      border: "border-slate-100 dark:border-slate-800",
      text: "text-slate-500 dark:text-slate-300",
      meaning: "Interrupted during shutdown or restart; the scheduler will retry later.",
    };
  }
  return {
    bg: "bg-emerald-50/70 dark:bg-emerald-950/20",
    border: "border-emerald-100 dark:border-emerald-900/40",
    text: "text-emerald-600 dark:text-emerald-300",
    meaning: "Last run completed.",
  };
}

function humanizeJobDetail(detail: string): string {
  const known: Record<string, string> = {
    no_open_questions: "No open research questions are ready for this job.",
    research_provider_not_configured: "External research is waiting on provider configuration, usually a missing Tavily key.",
    no_investigating_questions_ready: "No investigating questions are ready to resolve.",
    no_relation_review_candidate: "No relation-review candidate currently needs graph linking.",
    no_pending_evidence: "No pending evidence is waiting for extraction.",
    no_active_experiments: "No active shadow experiments need advancement.",
    runtime_disabled: "Disabled by integration settings.",
    shutdown_cancelled: "Cancelled because the app was shutting down.",
    gmail_credentials_missing: "Gmail sync is enabled but missing a username or app password.",
    insufficient_tracked_universe: "Pattern review needs at least two tracked holdings or watchlist names before it can compare relationships.",
    insufficient_independent_pattern_evidence: "Pattern review did not have enough independently sourced, current evidence to form a defensible hypothesis.",
    model_found_no_actionable_pattern: "The review found no sufficiently strong, portfolio-relevant pattern in the current evidence window.",
    duplicate_pattern_hypothesis: "The proposed pattern already exists, so Prophet kept the current hypothesis instead of creating a duplicate.",
    validated_pattern_preview: "A pattern passed validation in preview mode; no stored state was changed.",
  };
  if (detail.startsWith("already_advancing=")) {
    return "Another runner is already advancing this shadow experiment; no duplicate checkpoint was created.";
  }
  if (/ForeignKeyViolationError|IntegrityError|violates foreign key constraint/i.test(detail)) {
    return "This run stopped because another stored record still referenced an item being cleaned up. See the activity log for technical detail.";
  }
  if (/^(?:[a-z_]+=[^\s]+\s*)+$/.test(detail.trim())) {
    return detail
      .trim()
      .split(/\s+/)
      .map(part => {
        const [key, ...valueParts] = part.split("=");
        return `${formatStatusText(key)}: ${valueParts.join("=") || "0"}`;
      })
      .join(" · ");
  }
  return known[detail] ?? detail.replace(/_/g, " ");
}

function formatInterval(seconds?: number | null): string {
  if (!seconds) return "Manual";
  if (seconds < 60) return `Every ${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `Every ${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `Every ${hours}h`;
  const days = Math.round(hours / 24);
  return `Every ${days}d`;
}

function formatJobTime(value?: string | null): string {
  if (!value) return "Not run yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Last run time unknown";
  return `Last run ${date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
}

function TabButton({ active, onClick, label, icon }: { active: boolean; onClick: () => void; label: string; icon: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      className={`flex min-w-0 items-center justify-center gap-2 border-b-2 px-2 py-2.5 text-sm font-medium transition-colors sm:shrink-0 sm:justify-start sm:px-4 ${
        active
          ? "border-gray-900 text-gray-950 dark:border-gray-100 dark:text-gray-100"
          : "border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:hover:border-gray-700 dark:hover:text-gray-100"
      }`}
    >
      {icon}
      {label}
    </button>
  );
}

function Card({ title, icon, children }: { title: string; icon?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-gray-950">
      <div className="mb-5 flex items-center gap-2 border-b border-gray-100 pb-3 dark:border-gray-800">
        {icon}
        <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-200">{title}</h2>
      </div>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <label className="text-xs font-medium text-gray-600 dark:text-gray-300">{label}</label>
      {children}
    </div>
  );
}

function SecretStatus({ saved, pending }: { saved: boolean; pending: boolean }) {
  const tone = pending ? "text-amber-600 dark:text-amber-300" : saved ? "text-emerald-600 dark:text-emerald-300" : "text-gray-500 dark:text-gray-400";
  const label = pending ? "New credential pending save" : saved ? "Saved credential will be kept" : "Missing credential";
  return (
    <p className={`text-xs font-medium ${tone}`}>
      {label}
    </p>
  );
}

function StatItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="border-l-2 border-gray-200 py-1 pl-3 dark:border-gray-700">
      <p className="text-xs text-gray-500 dark:text-gray-400">{label}</p>
      <p className="mt-1 truncate text-lg font-semibold">{value}</p>
    </div>
  );
}

const inputClass = "w-full rounded-md border border-gray-300 bg-white px-3 py-2.5 text-sm transition-colors focus:border-gray-500 focus:outline-none focus:ring-2 focus:ring-gray-900/10 dark:border-gray-700 dark:bg-gray-900 dark:focus:border-gray-500 dark:focus:ring-white/10";

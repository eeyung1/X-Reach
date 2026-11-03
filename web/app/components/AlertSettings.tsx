"use client";

import { FormEvent, useState } from "react";

type RuleDraft = {
  key: string;
  ruleId: string;
  minViewsPerMinute: string;
  maxAgeMinutes: string;
  simclusters: string;
};

type WebhookDraft = {
  key: string;
  webhookId: string;
  url: string;
  secret: string;
  format: "generic" | "discord" | "slack";
};

type SavedConfiguration = {
  rules: Array<{
    rule_id: string;
    min_views_per_minute: number;
    max_age_minutes: number;
    simclusters: string[];
  }>;
  webhooks: Array<{ webhook_id: string; format: WebhookDraft["format"] }>;
};

type StatusTone = "idle" | "working" | "success" | "error";

const DEFAULT_RULE: RuleDraft = {
  key: "default-rule",
  ruleId: "reply-timing",
  minViewsPerMinute: "500",
  maxAgeMinutes: "30",
  simclusters: "",
};

const DEFAULT_WEBHOOK: WebhookDraft = {
  key: "default-webhook",
  webhookId: "team-chat",
  url: "",
  secret: "",
  format: "generic",
};

export default function AlertSettings() {
  const [adminToken, setAdminToken] = useState("");
  const [rules, setRules] = useState<RuleDraft[]>([{ ...DEFAULT_RULE }]);
  const [testRuleId, setTestRuleId] = useState(DEFAULT_RULE.ruleId);
  const [webhooks, setWebhooks] = useState<WebhookDraft[]>([DEFAULT_WEBHOOK]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<{ tone: StatusTone; message: string }>({
    tone: "idle",
    message: "Add a webhook and save your alert settings to get started.",
  });
  const [lastDeliveries, setLastDeliveries] = useState<Record<string, boolean> | null>(null);

  function requestHeaders() {
    return {
      "Content-Type": "application/json",
      "X-Alert-Admin-Token": adminToken.trim(),
    };
  }

  async function responseError(response: Response) {
    const payload = (await response.json().catch(() => null)) as
      | { detail?: string }
      | null;
    return payload?.detail || `Request failed (${response.status}).`;
  }

  async function loadConfiguration() {
    if (!adminToken.trim()) {
      setStatus({ tone: "error", message: "Enter the alert admin token first." });
      return;
    }
    setBusy(true);
    setStatus({ tone: "working", message: "Loading saved alert settings…" });
    setLastDeliveries(null);
    try {
      const response = await fetch("/api/v1/alerts", {
        headers: requestHeaders(),
        cache: "no-store",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const saved = (await response.json()) as SavedConfiguration;
      const configuredRules = saved.rules.map((configuredRule, index) => ({
        key: `saved-rule-${index}`,
        ruleId: configuredRule.rule_id,
        minViewsPerMinute: String(configuredRule.min_views_per_minute),
        maxAgeMinutes: String(configuredRule.max_age_minutes),
        simclusters: configuredRule.simclusters.join(", "),
      }));
      const nextRules = configuredRules.length
        ? configuredRules
        : [{ ...DEFAULT_RULE }];
      setRules(nextRules);
      setTestRuleId(nextRules[0].ruleId);
      setWebhooks(
        saved.webhooks.length
          ? saved.webhooks.map((webhook, index) => ({
              key: `saved-webhook-${index}`,
              webhookId: webhook.webhook_id,
              url: "",
              secret: "",
              format: webhook.format,
            }))
          : [{ ...DEFAULT_WEBHOOK }],
      );
      setStatus({
        tone: "success",
        message: saved.webhooks.length
          ? "Settings loaded. Re-enter each webhook URL and signing secret before saving changes."
          : "No saved webhooks yet. Add a destination and save your settings.",
      });
    } catch (error) {
      setStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "Could not load alert settings.",
      });
    } finally {
      setBusy(false);
    }
  }

  async function saveConfiguration(): Promise<boolean> {
    if (!adminToken.trim()) {
      setStatus({ tone: "error", message: "Enter the alert admin token first." });
      return false;
    }
    if (
      rules.some((rule) => {
        const minViews = Number(rule.minViewsPerMinute);
        const maxAge = Number(rule.maxAgeMinutes);
        return (
          !rule.ruleId.trim() ||
          !Number.isFinite(minViews) ||
          minViews < 0 ||
          !Number.isFinite(maxAge) ||
          maxAge <= 0
        );
      })
    ) {
      setStatus({ tone: "error", message: "Check each rule ID, velocity, and age limit." });
      return false;
    }
    if (new Set(rules.map((rule) => rule.ruleId.trim())).size !== rules.length) {
      setStatus({ tone: "error", message: "Each alert rule needs a unique ID." });
      return false;
    }
    if (webhooks.some((webhook) => !webhook.webhookId.trim() || !webhook.url.trim() || !webhook.secret)) {
      setStatus({ tone: "error", message: "Each destination needs an ID, URL, and signing secret." });
      return false;
    }

    setStatus({ tone: "working", message: "Saving alert settings…" });
    setLastDeliveries(null);
    const payload = {
      rules: rules.map((rule) => ({
          rule_id: rule.ruleId.trim(),
          min_views_per_minute: Number(rule.minViewsPerMinute),
          max_age_minutes: Number(rule.maxAgeMinutes),
          simclusters: rule.simclusters
            .split(",")
            .map((cluster) => cluster.trim())
            .filter(Boolean),
        })),
      webhooks: webhooks.map((webhook) => ({
        webhook_id: webhook.webhookId.trim(),
        url: webhook.url.trim(),
        secret: webhook.secret,
        format: webhook.format,
      })),
    };

    try {
      const response = await fetch("/api/v1/alerts", {
        method: "PUT",
        headers: requestHeaders(),
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setStatus({ tone: "success", message: "Alert settings saved." });
      return true;
    } catch (error) {
      setStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "Could not save alert settings.",
      });
      return false;
    }
  }

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    await saveConfiguration();
    setBusy(false);
  }

  async function sendTest() {
    if (busy) return;
    setBusy(true);
    if (!(await saveConfiguration())) {
      setBusy(false);
      return;
    }
    const testRule = rules.find((rule) => rule.ruleId === testRuleId) ?? rules[0];
    const minViews = Number(testRule.minViewsPerMinute);
    const maxAge = Number(testRule.maxAgeMinutes);
    const simclusters = testRule.simclusters
      .split(",")
      .map((cluster) => cluster.trim())
      .filter(Boolean);
    const seed = {
      tweet_id: `alert-test-${Date.now()}`,
      text: "X-Reach alert delivery test.",
      views_per_minute: minViews,
      age_minutes: Math.min(1, maxAge / 2),
      simclusters,
    };
    setStatus({ tone: "working", message: "Sending a test alert to configured destinations…" });
    try {
      const response = await fetch("/api/v1/alerts/test", {
        method: "POST",
        headers: requestHeaders(),
        body: JSON.stringify({ rule_id: testRule.ruleId.trim(), seed }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = (await response.json()) as {
        deliveries: Record<string, boolean>;
      };
      setLastDeliveries(result.deliveries);
      const allDelivered = Object.values(result.deliveries).every(Boolean);
      setStatus({
        tone: allDelivered ? "success" : "error",
        message: allDelivered
          ? "Test alert delivered to every destination."
          : "One or more destinations did not accept the test alert.",
      });
    } catch (error) {
      setStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "Could not send the test alert.",
      });
    } finally {
      setBusy(false);
    }
  }

  function markUnsaved() {
    setLastDeliveries(null);
    setStatus({ tone: "idle", message: "Unsaved changes. Save to update alert delivery." });
  }

  function updateRule(index: number, patch: Partial<RuleDraft>) {
    if (rules[index]?.ruleId === testRuleId && patch.ruleId !== undefined) {
      setTestRuleId(patch.ruleId);
    }
    setRules((current) =>
      current.map((rule, ruleIndex) =>
        ruleIndex === index ? { ...rule, ...patch } : rule,
      ),
    );
    markUnsaved();
  }

  function updateWebhook(index: number, patch: Partial<WebhookDraft>) {
    setWebhooks((current) =>
      current.map((webhook, itemIndex) =>
        itemIndex === index ? { ...webhook, ...patch } : webhook,
      ),
    );
    markUnsaved();
  }

  return (
    <section className="alert-panel panel" aria-labelledby="alert-title">
      <div className="panel-heading alert-heading">
        <div>
          <span className="step">04 / REPLY TIMING</span>
          <h2 id="alert-title">Autonomous reply alerts</h2>
        </div>
        <span className={`alert-status-badge ${status.tone}`}>
          <span className="alert-status-dot" />
          {status.tone === "success"
            ? "READY"
            : status.tone === "error"
              ? "CHECK SETTINGS"
              : status.tone === "working"
                ? "WORKING"
                : "SETUP"}
        </span>
      </div>

      <p className="alert-intro">
        Get a webhook when a fresh root post crosses your velocity and age thresholds.
      </p>

      <div className="alert-admin-row">
        <label className="alert-field alert-admin-field">
          <span>ALERT ADMIN TOKEN</span>
          <input
            type="password"
            autoComplete="new-password"
            value={adminToken}
            disabled={busy}
            onChange={(event) => setAdminToken(event.target.value)}
            placeholder="Enter the API admin token"
          />
        </label>
        <button
          className="alert-secondary-button"
          type="button"
          disabled={busy || !adminToken.trim()}
          onClick={loadConfiguration}
        >
          Load saved settings
        </button>
      </div>

      <form onSubmit={handleSave}>
        <fieldset className="alert-controls" disabled={busy}>
        <div className="alert-target-heading alert-rules-heading">
          <div>
            <span>ALERT RULES</span>
            <p>Send when a fresh post meets each rule’s thresholds.</p>
          </div>
          <button
            className="alert-secondary-button"
            type="button"
            disabled={rules.length >= 100}
            onClick={() => {
              setRules((current) => {
                const usedIds = new Set(current.map((rule) => rule.ruleId));
                const usedKeys = new Set(current.map((rule) => rule.key));
                let suffix = current.length + 1;
                while (
                  usedIds.has(`reply-timing-${suffix}`) ||
                  usedKeys.has(`new-rule-${suffix}`)
                ) {
                  suffix += 1;
                }
                return [
                  ...current,
                  {
                    ...DEFAULT_RULE,
                    key: `new-rule-${suffix}`,
                    ruleId: `reply-timing-${suffix}`,
                  },
                ];
              });
              markUnsaved();
            }}
          >
            Add rule
          </button>
        </div>
        <div className="alert-rule-list">
          {rules.map((rule, index) => (
            <fieldset className="alert-rule" key={rule.key}>
              <legend>RULE {String(index + 1).padStart(2, "0")}</legend>
              <div className="alert-config-grid">
                <label className="alert-field">
                  <span>RULE NAME</span>
                  <input
                    value={rule.ruleId}
                    maxLength={80}
                    onChange={(event) => updateRule(index, { ruleId: event.target.value })}
                    required
                  />
                </label>
                <label className="alert-field">
                  <span>MINIMUM VIEWS / MIN</span>
                  <input
                    type="number"
                    min="0"
                    step="any"
                    value={rule.minViewsPerMinute}
                    onChange={(event) => updateRule(index, { minViewsPerMinute: event.target.value })}
                    required
                  />
                </label>
                <label className="alert-field">
                  <span>MAXIMUM POST AGE (MIN)</span>
                  <input
                    type="number"
                    min="0.01"
                    step="any"
                    value={rule.maxAgeMinutes}
                    onChange={(event) => updateRule(index, { maxAgeMinutes: event.target.value })}
                    required
                  />
                </label>
                <label className="alert-field">
                  <span>SIMCLUSTERS · OPTIONAL</span>
                  <input
                    value={rule.simclusters}
                    onChange={(event) => updateRule(index, { simclusters: event.target.value })}
                    placeholder="Technology, Design"
                  />
                </label>
              </div>
              {rules.length > 1 && (
                <button
                  className="alert-remove-button"
                  type="button"
                  onClick={() => {
                    const remaining = rules.filter((_, ruleIndex) => ruleIndex !== index);
                    setRules(remaining);
                    if (testRuleId === rule.ruleId) setTestRuleId(remaining[0].ruleId);
                    markUnsaved();
                  }}
                  aria-label={`Remove rule ${rule.ruleId}`}
                >
                  Remove rule
                </button>
              )}
            </fieldset>
          ))}
        </div>

        <div className="alert-target-heading">
          <div>
            <span>WEBHOOK DESTINATIONS</span>
            <p>Signing secrets stay in this page session and are not returned by the API.</p>
          </div>
          <button
            className="alert-secondary-button"
            type="button"
            disabled={webhooks.length >= 100}
            onClick={() => {
              setWebhooks((current) => {
                const usedIds = new Set(current.map((webhook) => webhook.webhookId));
                const usedKeys = new Set(current.map((webhook) => webhook.key));
                let suffix = current.length + 1;
                while (
                  usedIds.has(`destination-${suffix}`) ||
                  usedKeys.has(`new-webhook-${suffix}`)
                ) {
                  suffix += 1;
                }
                return [
                  ...current,
                  {
                    ...DEFAULT_WEBHOOK,
                    key: `new-webhook-${suffix}`,
                    webhookId: `destination-${suffix}`,
                  },
                ];
              });
              markUnsaved();
            }}
          >
            Add destination
          </button>
        </div>

        <div className="alert-target-list">
          {webhooks.map((webhook, index) => (
            <fieldset className="alert-target" key={webhook.key}>
              <legend>DESTINATION {String(index + 1).padStart(2, "0")}</legend>
              <label className="alert-field">
                <span>DESTINATION ID</span>
                <input
                  value={webhook.webhookId}
                  maxLength={80}
                  onChange={(event) => updateWebhook(index, { webhookId: event.target.value })}
                  required
                />
              </label>
              <label className="alert-field">
                <span>FORMAT</span>
                <select
                  value={webhook.format}
                  onChange={(event) =>
                    updateWebhook(index, {
                      format: event.target.value as WebhookDraft["format"],
                    })
                  }
                >
                  <option value="generic">Generic JSON</option>
                  <option value="discord">Discord</option>
                  <option value="slack">Slack</option>
                </select>
              </label>
              <label className="alert-field alert-target-url">
                <span>WEBHOOK URL</span>
                <input
                  type="url"
                  value={webhook.url}
                  onChange={(event) => updateWebhook(index, { url: event.target.value })}
                  placeholder="https://hooks.example/…"
                  required
                />
              </label>
              <label className="alert-field alert-target-secret">
                <span>HMAC SIGNING SECRET</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={webhook.secret}
                  onChange={(event) => updateWebhook(index, { secret: event.target.value })}
                  required
                />
              </label>
              {webhooks.length > 1 && (
                <button
                  className="alert-remove-button"
                  type="button"
                  onClick={() => {
                    setWebhooks((current) => current.filter((_, itemIndex) => itemIndex !== index));
                    markUnsaved();
                  }}
                  aria-label={`Remove destination ${index + 1}`}
                >
                  Remove
                </button>
              )}
            </fieldset>
          ))}
        </div>

        </fieldset>
        <div className="alert-form-footer">
          <p className={`alert-message ${status.tone}`} role="status" aria-live="polite">
            {status.message}
          </p>
          <div className="alert-actions">
            <label className="alert-test-selector">
              <span>TEST RULE</span>
              <select
                value={testRuleId}
                onChange={(event) => setTestRuleId(event.target.value)}
              >
                {rules.map((rule) => (
                  <option key={rule.ruleId} value={rule.ruleId}>{rule.ruleId}</option>
                ))}
              </select>
            </label>
            <button className="alert-secondary-button" type="submit" disabled={busy}>
              {busy ? "Working…" : "Save settings"}
            </button>
            <button
              className="analyze-button"
              type="button"
              disabled={busy || webhooks.length === 0}
              onClick={sendTest}
            >
              {busy ? "Working…" : "Save & send test"}
              <span aria-hidden="true">↗</span>
            </button>
          </div>
        </div>
        {lastDeliveries && (
          <ul className="alert-delivery-list" aria-label="Last test delivery results">
            {Object.entries(lastDeliveries).map(([webhookId, delivered]) => (
              <li key={webhookId} className={delivered ? "delivered" : "not-delivered"}>
                <span aria-hidden="true">{delivered ? "✓" : "!"}</span>
                {webhookId}: {delivered ? "accepted" : "delivery failed"}
              </li>
            ))}
          </ul>
        )}
      </form>
    </section>
  );
}

"use strict";

// The dashboard is deliberately dependency-free and state-light: one run is
// loaded at a time, the server owns every decision, and the UI never derives
// a policy outcome of its own -- it only displays what the gate returned.

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const state = { run: null, runId: null, busy: false, livePlanner: null };

const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const rupees = (paise) => {
  const rounded = Math.round((paise || 0) / 100);
  return (rounded < 0 ? "−₹" : "₹") + inr.format(Math.abs(rounded));
};
const pct = (x) => (x === null || x === undefined ? "—" : (x * 100).toFixed(1) + "%");
const titleise = (s) => (s || "").replace(/_/g, " ");

// --- transport ---------------------------------------------------------

async function api(path, options) {
  const res = await fetch("/api" + path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error((body && (body.detail || body.message)) || res.statusText);
  }
  return body;
}

function status(message, kind) {
  const line = $("#status-line");
  line.textContent = message || "";
  line.className = "status-line" + (kind ? " " + kind : "");
}

async function withBusy(message, fn) {
  if (state.busy) return;
  state.busy = true;
  setButtonsDisabled(true);
  status(message, "busy");
  document.body.classList.remove("done");
  document.body.classList.add("busy");
  try {
    return await fn();
  } catch (err) {
    status(err.message, "err");
    throw err;
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    document.body.classList.add("done");
    setButtonsDisabled(false);
  }
}

function setButtonsDisabled(disabled) {
  const run = state.run;
  $("#btn-run").disabled = disabled;
  $("#btn-run-faults").disabled = disabled;
  $("#btn-verify").disabled = disabled || !run;
  $("#btn-approve").disabled =
    disabled || !run || run.status === "completed" || run.status === "halted";
  $("#btn-resume").disabled = disabled || !run || run.status !== "halted";
}

// --- run lifecycle -----------------------------------------------------

async function refreshRunList() {
  const runs = await api("/runs");
  const select = $("#run-select");
  select.innerHTML = "";
  if (!runs.length) {
    select.appendChild(el("option", null, "no runs yet"));
    select.disabled = true;
    return;
  }
  select.disabled = false;
  runs.forEach((run) => {
    const when = run.created_at ? new Date(run.created_at * 1000).toLocaleTimeString() : "";
    const option = el(
      "option",
      null,
      when + " · seed " + run.seed + (run.faults ? " · faults" : "") + " · " + run.status
    );
    option.value = run.run_id;
    select.appendChild(option);
  });
  select.value = state.runId || runs[0].run_id;
}

async function loadRun(runId) {
  const run = await api("/runs/" + runId);
  state.run = run;
  state.runId = run.run_id;
  render(run);
  return run;
}

async function startRun(faults) {
  await withBusy(
    faults ? "Running with injected gateway faults…" : "Extracting and planning…",
    async () => {
      const run = await api("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          seed: Number($("#seed").value) || 0,
          control_fraction: Number($("#control-fraction").value),
          faults: !!faults,
        }),
      });
      state.run = run;
      state.runId = run.run_id;
      render(run);
      await refreshRunList();
      status(
        "Prepared " + run.cases.length + " cases (" + run.progress.treated_total +
          " treated). Plans are proposed; nothing has been executed yet."
      );
    }
  );
}

// --- rendering ---------------------------------------------------------

function render(run) {
  $("#run-view").hidden = false;
  $("#empty-state").hidden = true;
  renderMeta(run);
  renderHalt(run);
  renderNextStep(run);
  renderCards(run);
  renderPlanner(run);
  renderPanels(run);
  renderCohorts(run);
  populateFilters(run);
  renderCases(run);
  setButtonsDisabled(false);
}

const STATUS_LABEL = {
  awaiting_approval: ["Awaiting approval", "info"],
  running: ["Running", "info"],
  halted: ["Halted", "bad"],
  completed: ["Completed", "ok"],
};

function metaItem(host, k, v, tag) {
  const item = el("div", "meta-item");
  item.appendChild(el("span", "k", k));
  const value = el("span", "v");
  if (tag) value.appendChild(el("span", "tag " + tag, v));
  else value.textContent = v;
  item.appendChild(value);
  host.appendChild(item);
  return item;
}

function renderMeta(run) {
  const host = $("#run-meta");
  host.innerHTML = "";
  const label = STATUS_LABEL[run.status] || [titleise(run.status), ""];
  metaItem(host, "Status", label[0], label[1]);
  metaItem(host, "Seed", String(run.seed));
  // Mode is only worth a slot when it is not the default; on a normal run it
  // is one more thing to read that says nothing.
  if (run.original_faults) metaItem(host, "Mode", "faults injected", "warn");
  metaItem(host, "Cases", run.cases.length + " (" + run.progress.treated_total + " treated)");
  metaItem(host, "Actions executed", String(run.actions_used));

  const done = run.progress.treated_resolved;
  const total = run.progress.treated_total || 1;
  const item = metaItem(host, "Review progress", done + " / " + run.progress.treated_total + " decided");
  item.className = "meta-item meta-progress";
  const bar = el("div", "bar");
  const fill = el("i");
  fill.style.width = Math.round((done / total) * 100) + "%";
  bar.appendChild(fill);
  item.appendChild(bar);
}

// --- next step ---------------------------------------------------------
//
// The dashboard is a two-phase workflow: plans are proposed, then a human
// approves them. Nothing on the screen said so, which left a first-time
// reader looking at a wall of counters and five equally-weighted buttons
// with no idea which one was the point. This names the next action and puts
// the button for it in the same block.

function renderNextStep(run) {
  const bar = $("#nextstep");
  const btn = $("#ns-action");
  const title = $("#ns-title");
  const body = $("#ns-body");

  btn.className = "btn cta";
  btn.onclick = null;

  if (run.status === "awaiting_approval") {
    const treated = run.cases.filter((c) => c.arm === "treated");
    let blocked = 0;
    treated.forEach((c) => (c.preview || []).forEach((v) => { if (!v.allowed) blocked++; }));
    title.textContent = "Nothing has run yet — approve the batch to execute it";
    body.innerHTML = "";
    body.appendChild(document.createTextNode(""));
    const b = el("b", null, String(treated.length) + " plans");
    body.appendChild(b);
    body.appendChild(document.createTextNode(
      " are proposed and waiting. Approving runs only what policy permits: the gate re-checks " +
      "every action against live state immediately before it fires, so approval cannot push a " +
      "denied action through. " + blocked + " actions are already blocked on today's state."
    ));
    btn.textContent = "Approve all policy-allowed plans";
    btn.classList.add("is-good");
    btn.onclick = () => $("#btn-approve").click();
    bar.hidden = false;
    return;
  }

  if (run.status === "halted") {
    title.textContent = "Batch halted by the circuit breaker";
    body.textContent =
      "Completed work is checkpointed and will not be repeated. Resuming resets the breaker, " +
      "which is deliberately a human action — a breaker that reset itself would re-trip against " +
      "a still-broken gateway.";
    btn.textContent = "Resume from checkpoint";
    btn.classList.add("is-warn");
    btn.onclick = () => $("#btn-resume").click();
    bar.hidden = false;
    return;
  }

  if (run.status === "completed") {
    const m = run.metrics;
    const sig = m && m.recovery_rates.lift.significant;
    title.textContent = "Run complete — every decision is on the audit chain";
    body.textContent = sig
      ? "The lift interval stays on one side of zero on this run. Verify the chain to confirm no " +
        "record was altered, then open any case to see the nine-rule verdict behind it."
      : "The lift interval crosses zero, so this run does not show the intervention beat doing " +
        "nothing — which is reported rather than hidden. Verify the chain to confirm no record " +
        "was altered, then open any case to see the nine-rule verdict behind it.";
    btn.textContent = "Verify hash chain";
    btn.onclick = () => $("#btn-verify").click();
    bar.hidden = false;
    return;
  }

  bar.hidden = true;
}

function renderHalt(run) {
  const banner = $("#halt-banner");
  if (run.status === "halted") {
    banner.hidden = false;
    banner.innerHTML = "";
    banner.appendChild(el("b", null, "Batch halted — circuit breaker open. "));
    banner.appendChild(
      document.createTextNode(
        (run.halt_reason || "") +
          ". Completed work is checkpointed and will not be repeated. " +
          "Resuming resets the breaker, and is a deliberate human action."
      )
    );
  } else {
    banner.hidden = true;
  }
}

function statRow(host, label, value, note, tone) {
  const row = el("div", "stat");
  const k = el("span", "k", label);
  if (note) k.appendChild(el("small", null, note));
  row.appendChild(k);
  row.appendChild(el("span", "v" + (tone ? " " + tone : ""), value));
  host.appendChild(row);
}

function barRow(host, label, note, value, scale, tone) {
  const row = el("div", "barrow");
  const lbl = el("span", "lbl", label);
  if (note) lbl.appendChild(el("small", null, note));
  row.appendChild(lbl);
  const track = el("div", "track");
  const fill = el("i", tone);
  fill.style.width = Math.max(1, Math.round((value / scale) * 100)) + "%";
  track.appendChild(fill);
  row.appendChild(track);
  row.appendChild(el("span", "val", pct(value)));
  host.appendChild(row);
}

function panel(host, title, lead) {
  const p = el("article", "mpanel" + (lead ? " lead" : ""));
  p.appendChild(el("h3", null, title));
  host.appendChild(p);
  return p;
}

function hero(host, value, unit, tone) {
  const h = el("div", "hero");
  h.appendChild(el("span", "n" + (tone ? " " + tone : ""), value));
  if (unit) h.appendChild(el("span", "u", unit));
  host.appendChild(h);
  return h;
}

function renderCards(run) {
  const host = $("#metrics");
  host.innerHTML = "";
  const m = run.metrics;
  if (!m) {
    renderPreflight(run, host);
    return;
  }

  const rates = m.recovery_rates;
  const lift = rates.lift;
  const money = m.money_paise;
  const sig = lift.significant;

  // --- did it work? one shared scale for both arms ------------------------
  const p1 = panel(host, "Did contacting them help?", true);
  const scale = Math.max(rates.treated, rates.control_self_recovery, 0.01) * 1.25;
  const bars = el("div", "bars");
  barRow(bars, "We contacted", m.arms.treated + " cases", rates.treated, scale, "");
  barRow(bars, "We left alone", m.arms.control + " cases", rates.control_self_recovery, scale, "neutral");
  p1.appendChild(bars);
  p1.appendChild(el("div", "scale-note", "share that paid in the end · axis ends at " + pct(scale)));
  p1.appendChild(el("div", "mrule"));

  const liftHero = hero(
    p1,
    (lift.point >= 0 ? "+" : "") + (lift.point * 100).toFixed(1) + " pts",
    "difference between the arms",
    sig ? (lift.point > 0 ? "good" : "bad") : "muted"
  );
  liftHero.appendChild(
    el("span", "tag " + (sig ? "ok" : ""), sig ? "significant" : "not significant")
  );
  p1.appendChild(
    el(
      "p",
      "mnote",
      "95% confidence interval " + pct(lift.ci_low) + " to " + pct(lift.ci_high) +
        (sig
          ? ". The interval stays on one side of zero, so the difference holds up."
          : ". The interval crosses zero, so this run does not show the nudging beat doing nothing.")
    )
  );

  // --- what was it worth? --------------------------------------------------
  const p2 = panel(host, "What it was worth", true);
  hero(p2, rupees(money.incremental_recovered), "incremental — what we caused", sig ? "good" : "muted");
  p2.appendChild(
    el("p", "mnote", "95% CI " + rupees(money.incremental_ci[0]) + " to " + rupees(money.incremental_ci[1]))
  );
  p2.appendChild(el("div", "mrule"));

  const stats = el("div", "stats");
  statRow(stats, "Gross recovered", rupees(money.gross_recovered),
    "Everyone who paid, including those who would have anyway.", "muted");
  statRow(stats, "Overstatement", money.overstatement_factor + "×",
    "How far a gross headline would exaggerate the impact.", "bad");
  statRow(stats, "Cost of actions", rupees(m.cost_paise.total),
    (m.cost_paise.per_rupee_recovered * 100).toFixed(2) + "% of the money recovered.");
  statRow(stats, "Wasted contacts", String(m.false_positives.wasted_contacts),
    "Messaged people who were going to pay regardless.");
  p2.appendChild(stats);

  const caveat = $("#metric-caveat");
  caveat.innerHTML = "";
  caveat.appendChild(el("b", null, sig ? "Read carefully. " : "Not statistically significant. "));
  caveat.appendChild(
    document.createTextNode(
      sig
        ? "Outcomes come from a seeded simulator, not production traffic. The control arm is the only reason the incremental number means anything."
        : "The confidence interval for lift crosses zero, so this run does not demonstrate that the intervention beat doing nothing. Reporting the gross number as impact would be a claim the data does not support."
    )
  );
}

// Before anything is approved there are no outcomes to report, but there is
// plenty worth showing: what was found, what is proposed, and what the gate
// already says about it. The preview verdicts come from the policy engine
// itself -- the UI never decides an outcome of its own.
function renderPreflight(run, host) {
  const atRisk = run.cases.reduce((sum, c) => sum + c.amount_paise, 0);
  const treated = run.cases.filter((c) => c.arm === "treated");
  let proposed = 0;
  let allowed = 0;
  let blocked = 0;
  treated.forEach((c) => {
    (c.preview || []).forEach((step) => {
      proposed += 1;
      if (step.allowed) allowed += 1;
      else blocked += 1;
    });
  });

  const p1 = panel(host, "What the batch found", true);
  hero(p1, rupees(atRisk), "at risk across " + run.cases.length + " cases");
  p1.appendChild(el("div", "mrule"));
  const s1 = el("div", "stats");
  statRow(s1, "Actionable", String(run.extraction.actionable),
    "Failures the taxonomy recognises and can act on.");
  statRow(s1, "Unclassified", String(run.extraction.unclassified_exceptions),
    "Reason not in the taxonomy — these go to a human, never to automation.");
  statRow(s1, "Held back as control", String(run.cases.length - treated.length),
    "Never contacted, so the recovery number stays checkable.");
  p1.appendChild(s1);

  const p2 = panel(host, "What is proposed", true);
  hero(p2, String(proposed), "actions across " + treated.length + " plans");
  p2.appendChild(el("p", "mnote", "Nothing has run. Every plan is waiting for your decision."));
  p2.appendChild(el("div", "mrule"));
  const s2 = el("div", "stats");
  statRow(s2, "Policy would allow", String(allowed),
    "Re-checked against live state immediately before each action runs.", "good");
  statRow(s2, "Policy would block", String(blocked),
    "Denials and escalations the gate returns on today's state.", blocked ? "bad" : "muted");
  p2.appendChild(s2);

  const caveat = $("#metric-caveat");
  caveat.innerHTML = "";
  caveat.appendChild(el("b", null, "Nothing has run yet. "));
  caveat.appendChild(
    document.createTextNode(
      "These are proposals and a policy preview, not outcomes. Recovery, lift and money " +
        "figures appear once approved plans have executed — and the control arm stays untouched throughout."
    )
  );
}

function kvRow(host, k, v, cls) {
  const row = el("div", "kv");
  row.appendChild(el("span", "k", k));
  row.appendChild(el("span", "v" + (cls ? " " + cls : ""), String(v)));
  host.appendChild(row);
  return row;
}

// --- planner panel -----------------------------------------------------
//
// The dashboard was showing only half the thesis. Every panel described what
// the policy engine refused, and nothing showed what the planner proposed or
// why -- so the boundary the whole system is built around was invisible.
//
// This puts the two halves side by side on one row: the model's chosen
// actions and its written reasoning on the left, the gate's verdict on the
// right. Cases where the gate overruled the planner are shown first, because
// those are the ones that demonstrate the bound actually binds.

const VERDICT_TONE = { allow: "allow", deny: "deny", escalate: "escalate" };

function plannerSourceLabel(source) {
  if (source === "llm") return "Claude";
  if (source === "llm_fallback") return "fallback";
  return "deterministic";
}

function renderPlanner(run) {
  const panel = $("#planner-panel");
  const planned = (run.cases || []).filter((c) => c.plan && c.plan.actions.length);
  if (!planned.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  // --- stats -----------------------------------------------------------
  const bySource = {};
  planned.forEach((c) => {
    const k = plannerSourceLabel(c.plan.source);
    bySource[k] = (bySource[k] || 0) + 1;
  });
  const proposedActions = planned.reduce((n, c) => n + c.plan.actions.length, 0);
  let blocked = 0;
  planned.forEach((c) => {
    (c.preview || []).forEach((row) => {
      if (!row.allowed) blocked++;
    });
  });

  const stats = $("#planner-stats");
  stats.innerHTML = "";
  const primary = Object.keys(bySource).sort((a, b) => bySource[b] - bySource[a])[0];
  [
    ["is-llm", planned.length, "plans proposed"],
    ["", proposedActions, "actions in them"],
    ["is-block", blocked, "overruled by policy"],
  ].forEach(([cls, v, l]) => {
    const d = el("div", "planner-stat " + cls);
    d.appendChild(el("div", "ps-v", String(v)));
    d.appendChild(el("div", "ps-l", l));
    stats.appendChild(d);
  });
  const src = el("div", "planner-stat");
  src.appendChild(el("div", "ps-v", primary === "Claude" ? "Claude" : "table"));
  src.appendChild(el("div", "ps-l", "planner in use"));
  stats.appendChild(src);

  // --- rows: overruled cases first ------------------------------------
  const overruled = (c) => (c.preview || []).some((r) => !r.allowed);
  const ordered = planned
    .slice()
    .sort((a, b) => {
      const d = Number(overruled(b)) - Number(overruled(a));
      if (d) return d;
      return b.amount_paise - a.amount_paise;
    })
    .slice(0, 6);

  const host = $("#planner-rows");
  host.innerHTML = "";
  ordered.forEach((c) => {
    const row = el("div", "planner-row");

    const left = el("div");
    left.appendChild(el("div", "pr-label", "Planner proposed"));
    left.appendChild(el("div", "pr-case", c.case_id + " · " + titleise(c.case_class)));
    left.appendChild(el("div", "pr-actions", c.plan.actions.map(titleise).join(" → ")));
    left.appendChild(el("div", "pr-why", c.plan.rationale || "no rationale recorded"));
    row.appendChild(left);

    row.appendChild(el("div", "divider"));

    const right = el("div");
    right.appendChild(el("div", "pr-label", "Policy engine returned"));
    const list = el("div", "pr-verdicts");
    (c.preview || []).forEach((v) => {
      const line = el("div", "pr-verdict");
      line.appendChild(el("span", "pv-tag " + (VERDICT_TONE[v.decision] || ""), v.decision));
      const why = el("span", "pv-why");
      const rule = (v.checks || []).find((k) => k.decision !== "allow");
      if (rule) {
        why.appendChild(el("b", null, rule.rule_id));
        why.appendChild(document.createTextNode(" — " + v.reason));
      } else {
        why.textContent = v.reason;
      }
      line.appendChild(why);
      list.appendChild(line);
    });
    right.appendChild(list);
    row.appendChild(right);

    row.onclick = () => openCase(c.case_id);
    host.appendChild(row);
  });

  const overruledCount = planned.filter(overruled).length;
  $("#planner-foot").textContent =
    "Showing " + ordered.length + " of " + planned.length + " plans, cases the gate overruled first. " +
    overruledCount + " of " + planned.length + " plans had at least one action refused. " +
    "Click any row for the full nine-rule verdict. The planner never sees these rules and cannot override them.";
}

function renderPanels(run) {
  const denials = $("#panel-denials");
  denials.innerHTML = "";
  const entries = Object.entries(run.denials || {});
  if (!entries.length) {
    denials.appendChild(el("div", "empty", "No policy denials yet."));
  } else {
    entries.forEach(([rule, count]) => kvRow(denials, titleise(rule), count));
  }

  const integrity = $("#panel-integrity");
  integrity.innerHTML = "";
  const audit = run.audit || {};
  const breaker = run.breaker || {};
  const records =
    audit.records_checked === null || audit.records_checked === undefined
      ? "—"
      : audit.records_checked;
  [
    ["Run status", titleise(run.status)],
    ["Audit chain", audit.description || "not verified yet"],
    ["Ledger records", records],
    ["Circuit breaker", titleise(breaker.state) + " (" + pct(breaker.failure_rate) + " failure rate)"],
    ["Actions executed", run.actions_used],
    [
      "Cases reviewed",
      (run.progress ? run.progress.treated_resolved : 0) + " / " + (run.progress ? run.progress.treated_total : 0),
    ],
    ["Seed", run.seed],
    ["Mode", run.original_faults ? "fault-injected simulator" : "simulator"],
  ].forEach(([k, v]) => kvRow(integrity, k, v));

  const exceptions = $("#panel-exceptions");
  exceptions.innerHTML = "";
  const escalated = run.cases.filter((c) => c.status === "escalated");
  const excepted = run.exceptions || [];
  if (!escalated.length && !excepted.length) {
    exceptions.appendChild(el("div", "empty", "No escalations or exceptions."));
  } else {
    escalated.forEach((c) => {
      const row = el("div", "kv");
      row.appendChild(el("span", "k mono", c.case_id));
      const v = el("span", "v");
      v.appendChild(el("span", "tag warn", "escalated"));
      row.appendChild(v);
      row.dataset.clickable = "1";
      row.onclick = () => openCase(c.case_id);
      exceptions.appendChild(row);
    });
    excepted.forEach((x) => {
      const row = kvRow(exceptions, x.case_id, x.reason, "hint");
      row.firstChild.className = "k mono";
      row.dataset.clickable = "1";
      row.onclick = () => openCase(x.case_id);
    });
  }

  const dead = $("#panel-deadletters");
  dead.innerHTML = "";
  if (!(run.dead_letters || []).length) {
    dead.appendChild(el("div", "empty", "No dead letters."));
  } else {
    run.dead_letters.forEach((d) => {
      const row = kvRow(
        dead,
        d.case_id + " · " + titleise(d.action),
        d.attempts + " attempts · " + d.last_error,
        "hint"
      );
      row.firstChild.className = "k mono";
      row.dataset.clickable = "1";
      row.onclick = () => openCase(d.case_id);
    });
  }
}

function renderCohorts(run) {
  const host = $("#panel-cohorts");
  host.innerHTML = "";
  if (!run.metrics) {
    host.appendChild(el("div", "empty", "No outcomes yet."));
    return;
  }
  const table = el("table");
  const head = el("thead");
  head.innerHTML =
    "<tr><th>Class</th><th class='num'>Treated</th><th class='num'>Control</th>" +
    "<th class='num'>Treated rate</th><th class='num'>Control rate</th>" +
    "<th class='num'>Lift (95% CI)</th></tr>";
  table.appendChild(head);
  const body = el("tbody");
  Object.entries(run.metrics.per_class).forEach(([cls, v]) => {
    const tr = el("tr");
    tr.style.cursor = "default";
    tr.appendChild(el("td", null, titleise(cls)));
    if (v.note) {
      const note = el("td", "hint", v.note);
      note.colSpan = 5;
      tr.appendChild(note);
    } else {
      tr.appendChild(el("td", "num", String(v.n_treated)));
      tr.appendChild(el("td", "num", String(v.n_control)));
      tr.appendChild(el("td", "num", pct(v.treated_rate)));
      tr.appendChild(el("td", "num", pct(v.control_rate)));
      const cell = el("td", "num");
      cell.appendChild(
        el(
          "span",
          "tag " + (v.lift.significant ? (v.lift.point > 0 ? "ok" : "bad") : ""),
          (v.lift.point >= 0 ? "+" : "") + pct(v.lift.point) +
            " (" + pct(v.lift.ci_low) + " – " + pct(v.lift.ci_high) + ")"
        )
      );
      tr.appendChild(cell);
    }
    body.appendChild(tr);
  });
  table.appendChild(body);
  host.appendChild(table);
}

// --- case table --------------------------------------------------------

function blockingRules(c) {
  const rules = new Set();
  (c.preview || []).concat(c.action_history || []).forEach((step) => {
    if (step.allowed) return;
    (step.checks || []).forEach((check) => {
      if (check.decision !== "allow") rules.add(check.rule_id);
    });
  });
  return rules;
}

function fillSelect(sel, values, allLabel) {
  const node = $(sel);
  const previous = node.value;
  const sorted = [...values].sort();
  node.innerHTML = "";
  const all = el("option", null, allLabel);
  all.value = "";
  node.appendChild(all);
  sorted.forEach((v) => {
    const option = el("option", null, titleise(v));
    option.value = v;
    node.appendChild(option);
  });
  node.value = sorted.includes(previous) ? previous : "";
}

function populateFilters(run) {
  const classes = new Set();
  const statuses = new Set();
  const denialRules = new Set();
  run.cases.forEach((c) => {
    classes.add(c.case_class);
    statuses.add(c.status);
    blockingRules(c).forEach((r) => denialRules.add(r));
  });
  fillSelect("#f-class", classes, "All classes");
  fillSelect("#f-status", statuses, "All statuses");
  fillSelect("#f-denial", denialRules, "Any policy outcome");
}

const STATUS_TONE = {
  executed: "ok",
  closed: "",
  rejected: "bad",
  dead_lettered: "bad",
  escalated: "warn",
  policy_blocked: "warn",
  halted: "bad",
  awaiting_review: "info",
  approved: "info",
  control: "",
};

function filteredCases(run) {
  const q = $("#f-search").value.trim().toLowerCase();
  const cls = $("#f-class").value;
  const st = $("#f-status").value;
  const arm = $("#f-arm").value;
  const denial = $("#f-denial").value;
  const minAmount = Number($("#f-min-amount").value) || 0;
  const exceptionsOnly = $("#f-exceptions").checked;

  return run.cases.filter((c) => {
    if (cls && c.case_class !== cls) return false;
    if (st && c.status !== st) return false;
    if (arm && c.arm !== arm) return false;
    if (minAmount && c.amount_paise < minAmount * 100) return false;
    if (exceptionsOnly && !c.exception) return false;
    if (denial && !blockingRules(c).has(denial)) return false;
    if (q) {
      const hay = [
        c.case_id,
        c.customer_id,
        c.case_class,
        c.kind,
        c.status,
        c.classification && c.classification.error_reason,
        c.plan && c.plan.rationale,
      ]
        .join(" ")
        .toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function renderCases(run) {
  const rows = filteredCases(run);
  const body = $("#case-table tbody");
  body.innerHTML = "";
  $("#case-count").textContent =
    rows.length + " of " + run.cases.length + " shown · " +
    (run.progress ? run.progress.awaiting_review : 0) + " awaiting review";

  rows.slice(0, 500).forEach((c) => {
    const tr = el("tr");
    tr.appendChild(el("td", "mono", c.case_id));
    tr.appendChild(el("td", null, titleise(c.case_class)));
    const armCell = el("td");
    armCell.appendChild(el("span", "tag " + (c.arm === "control" ? "" : "info"), c.arm));
    tr.appendChild(armCell);
    tr.appendChild(el("td", "num", rupees(c.amount_paise)));
    tr.appendChild(el("td", "num", c.age_days + "d"));
    tr.appendChild(el("td", null, c.plan ? c.plan.actions.map(titleise).join(", ") : "—"));
    const statusCell = el("td");
    statusCell.appendChild(el("span", "tag " + (STATUS_TONE[c.status] || ""), titleise(c.status)));
    tr.appendChild(statusCell);
    const outcomeCell = el("td");
    if (c.outcome) {
      outcomeCell.appendChild(
        el(
          "span",
          "tag " + (c.outcome.recovered ? "ok" : ""),
          c.outcome.recovered ? "recovered" : "not recovered"
        )
      );
    } else {
      outcomeCell.textContent = "—";
    }
    tr.appendChild(outcomeCell);
    tr.onclick = () => openCase(c.case_id);
    body.appendChild(tr);
  });

  if (rows.length > 500) {
    const tr = el("tr");
    const td = el("td", "hint", "Showing the first 500 of " + rows.length + " matching cases.");
    td.colSpan = 8;
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

// --- case drawer -------------------------------------------------------

async function openCase(caseId) {
  try {
    const c = await api("/runs/" + state.runId + "/cases/" + encodeURIComponent(caseId));
    renderDrawer(c);
  } catch (err) {
    status(err.message, "err");
  }
}

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#drawer-scrim").hidden = true;
  document.body.style.overflow = "";
}

function decisionTone(decision) {
  return decision === "allow" ? "ok" : decision === "escalate" ? "warn" : "bad";
}

function checksBlock(step) {
  const wrap = el("div", "section");
  const head = el("div", "check-row");
  head.appendChild(el("b", null, step.logical_attempt + ". " + titleise(step.action)));
  head.appendChild(
    el("span", "tag " + (step.allowed ? "ok" : decisionTone(step.decision)), step.decision)
  );
  if (step.retryable_later) head.appendChild(el("span", "tag", "retryable later"));
  wrap.appendChild(head);
  if (step.reason) wrap.appendChild(el("div", "reason", step.reason));
  (step.checks || []).forEach((check) => {
    const row = el("div", "check-row");
    row.appendChild(el("span", "tag " + decisionTone(check.decision), check.rule_id));
    row.appendChild(el("span", "reason", check.reason));
    wrap.appendChild(row);
  });
  if (step.execution) {
    const exec = el("div", "check-row");
    exec.appendChild(
      el("span", "tag " + (step.execution.ok ? "ok" : "bad"), step.execution.ok ? "executed" : "failed")
    );
    exec.appendChild(
      el(
        "span",
        "reason",
        step.execution.attempts + " transport attempt(s)" +
          (step.execution.error ? " · " + step.execution.error : "") +
          (step.execution.dead_lettered ? " · dead-lettered" : "")
      )
    );
    wrap.appendChild(exec);
  }
  return wrap;
}

function renderDrawer(c) {
  const drawer = $("#drawer");
  drawer.innerHTML = "";
  drawer.hidden = false;
  drawer.scrollTop = 0;
  $("#drawer-scrim").hidden = false;
  document.body.style.overflow = "hidden";

  const close = el("button", "btn tiny close", "Close");
  close.onclick = closeDrawer;
  drawer.appendChild(close);

  drawer.appendChild(el("h2", null, c.case_id));

  const summary = el("div", "section");
  [
    ["Source", titleise(c.kind) + " · " + (c.source_id || "—")],
    ["Customer", c.customer_id || "unknown"],
    ["Amount", rupees(c.amount_paise)],
    ["Age", c.age_days + " days"],
    ["Class", titleise(c.case_class)],
    ["Arm", c.arm],
    ["Actionable", c.actionable ? "yes" : "no"],
  ].forEach(([k, v]) => kvRow(summary, k, v));
  const stops = Object.entries(c.hard_stops || {}).filter(([, v]) => v);
  if (stops.length) {
    const row = el("div", "kv");
    row.appendChild(el("span", "k", "Hard stops"));
    const v = el("span", "v");
    stops.forEach(([k]) => v.appendChild(el("span", "tag bad", titleise(k))));
    row.appendChild(v);
    summary.appendChild(row);
  }
  drawer.appendChild(summary);

  if (c.classification) {
    drawer.appendChild(el("h3", null, "Classification"));
    const cl = el("div", "section");
    cl.appendChild(
      el("div", null, titleise(c.classification.recovery_class) + " — " + c.classification.grounding)
    );
    cl.appendChild(el("div", "reason", c.classification.rationale));
    cl.appendChild(
      el(
        "div",
        "hint",
        "error reason: " + (c.classification.error_reason || "—") +
          " · max retries " + c.classification.max_retries +
          " · contact " + (c.classification.allows_contact ? "allowed" : "not allowed") +
          " · silent retry " + (c.classification.can_retry_silently ? "allowed" : "not allowed")
      )
    );
    drawer.appendChild(cl);
  }

  if (c.arm === "control") {
    drawer.appendChild(el("h3", null, "Control arm"));
    const note = el("div", "section");
    note.appendChild(
      el(
        "div",
        "hint",
        "This case is in the control arm. It is never planned for, contacted or retried — that is what makes the lift number meaningful."
      )
    );
    drawer.appendChild(note);
  }

  if (c.plan) {
    drawer.appendChild(el("h3", null, "Proposed plan"));
    const plan = el("div", "section");
    plan.appendChild(el("div", null, c.plan.actions.map(titleise).join(" → ") || "no actions"));
    plan.appendChild(el("div", "reason", c.plan.rationale));
    plan.appendChild(el("div", "hint", "source: " + c.plan.source));

    // A live model is only offered when the server actually has one wired up,
    // and only while the case can still be re-planned. Offering a button that
    // is going to 409 is worse than not offering it.
    const replannable = c.status === "awaiting_review" || c.status === "halted";
    if (state.livePlanner && c.arm === "treated" && replannable) {
      const ask = el("button", "btn tiny", "Ask " + state.livePlanner + " to re-plan");
      ask.onclick = async () => {
        ask.disabled = true;
        ask.textContent = "Thinking…";
        try {
          const fresh = await api(
            "/runs/" + state.runId + "/cases/" + c.case_id + "/replan",
            { method: "POST" }
          );
          renderDrawer(fresh);
          state.run = await api("/runs/" + state.runId);
          render(state.run);
          status("Re-planned " + c.case_id + " with " + state.livePlanner +
                 ". The proposal is new; the nine rules that judged it are not.");
        } catch (err) {
          ask.disabled = false;
          ask.textContent = "Ask " + state.livePlanner + " to re-plan";
          status(err.message, "err");
        }
      };
      const foot = el("div", "decision-row");
      foot.appendChild(ask);
      foot.appendChild(el("span", "hint",
        "One call, this case only. The plan it returns is gated by the same nine rules."));
      plan.appendChild(foot);
    }
    drawer.appendChild(plan);
  }

  if ((c.preview || []).length) {
    drawer.appendChild(el("h3", null, "Policy preview — what the gate says now"));
    c.preview.forEach((step) => drawer.appendChild(checksBlock(step)));
  }

  drawer.appendChild(el("h3", null, "Operator decision"));
  const decisionBox = el("div", "section");
  if (c.decision) {
    decisionBox.appendChild(
      el(
        "div",
        null,
        "Decision: " + titleise(c.decision) + (c.decision_note ? " — " + c.decision_note : "")
      )
    );
  } else if (c.arm !== "treated") {
    decisionBox.appendChild(
      el("div", "hint", "Control cases are observation-only and cannot be decided.")
    );
  } else {
    decisionBox.appendChild(
      el(
        "div",
        "hint",
        "Approving means approving the proposed plan. Every action is re-gated by policy immediately before it runs; approval cannot force a denied action through."
      )
    );
    const row = el("div", "decision-row");
    const note = el("input");
    note.type = "text";
    note.placeholder = "Optional note for the audit trail";
    row.appendChild(note);
    ["approve", "escalate", "reject"].forEach((decision) => {
      const tone = decision === "approve" ? "good" : decision === "escalate" ? "warn" : "danger";
      const btn = el("button", "btn tiny " + tone, titleise(decision));
      btn.onclick = () => decide(c.case_id, decision, note.value);
      row.appendChild(btn);
    });
    decisionBox.appendChild(row);
  }
  drawer.appendChild(decisionBox);

  if ((c.action_history || []).length) {
    drawer.appendChild(el("h3", null, "Execution"));
    c.action_history.forEach((step) => drawer.appendChild(checksBlock(step)));
  }

  if (c.exception) {
    drawer.appendChild(el("h3", null, "Exception"));
    const box = el("div", "section");
    box.appendChild(el("div", "reason", c.exception.reason));
    drawer.appendChild(box);
  }

  if (c.outcome) {
    drawer.appendChild(el("h3", null, "Simulated outcome"));
    const box = el("div", "section");
    [
      ["Recovered", c.outcome.recovered ? "yes" : "no"],
      ["Contacts sent", c.outcome.contacts_sent],
      ["Retries attempted", c.outcome.retries_attempted],
      ["Action cost", rupees(c.outcome.cost_paise)],
    ].forEach(([k, v]) => kvRow(box, k, v));
    drawer.appendChild(box);
  }

  if ((c.timeline || []).length) {
    drawer.appendChild(el("h3", null, "Audit timeline"));
    const timeline = el("div", "timeline");
    c.timeline.forEach((ev) => {
      const item = el("div", "ev");
      const head = el("div", "head");
      const actorTone = ev.actor === "human" ? "info" : ev.actor === "policy" ? "warn" : "";
      head.appendChild(el("span", "tag " + actorTone, ev.actor));
      head.appendChild(el("b", null, titleise(ev.event)));
      head.appendChild(el("span", "hint mono", "seq " + ev.seq));
      item.appendChild(head);
      if (ev.reason) item.appendChild(el("div", "reason", ev.reason));
      if (ev.idempotency_key) item.appendChild(el("div", "hint mono", ev.idempotency_key));
      timeline.appendChild(item);
    });
    drawer.appendChild(timeline);
  }
}

async function decide(caseId, decision, note) {
  await withBusy("Recording " + decision + "…", async () => {
    const run = await api(
      "/runs/" + state.runId + "/cases/" + encodeURIComponent(caseId) + "/decision",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: decision, note: note }),
      }
    );
    state.run = run;
    render(run);
    status("Case " + caseId + ": " + decision + " recorded as a human audit event.");
    await openCase(caseId);
  });
}

// --- wiring ------------------------------------------------------------

function bind() {
  $("#btn-run").onclick = () => startRun(false);
  $("#btn-run-faults").onclick = () => startRun(true);

  $("#btn-approve").onclick = () =>
    withBusy("Re-gating and executing every policy-allowed plan…", async () => {
      const run = await api("/runs/" + state.runId + "/approve-safe", { method: "POST" });
      state.run = run;
      render(run);
      await refreshRunList();
      const blocks = Object.values(run.denials || {}).reduce((a, b) => a + b, 0);
      status(
        run.status === "halted"
          ? "Halted mid-batch: the circuit breaker opened. Inspect, then resume."
          : "Batch complete. " + run.actions_used + " actions executed, " + blocks + " policy blocks."
      );
    });

  $("#btn-resume").onclick = () =>
    withBusy("Resuming from the last checkpoint…", async () => {
      const run = await api("/runs/" + state.runId + "/resume", { method: "POST" });
      state.run = run;
      render(run);
      status(
        "Resumed. Completed work was not repeated and the ledger continued from its last hash."
      );
    });

  $("#btn-verify").onclick = () =>
    withBusy("Walking the hash chain…", async () => {
      const result = await api("/runs/" + state.runId + "/verify");
      await loadRun(state.runId);
      status(result.description, result.ok ? "" : "err");
    });

  $("#run-select").onchange = (e) =>
    withBusy("Loading run…", async () => {
      await loadRun(e.target.value);
      status("");
    });

  const filters = [
    "#f-search", "#f-class", "#f-status", "#f-arm",
    "#f-denial", "#f-min-amount", "#f-exceptions",
  ];
  filters.forEach((id) => {
    $(id).addEventListener("input", () => state.run && renderCases(state.run));
    $(id).addEventListener("change", () => state.run && renderCases(state.run));
  });
  $("#f-reset").onclick = () => {
    filters.slice(0, 6).forEach((id) => ($(id).value = ""));
    $("#f-exceptions").checked = false;
    if (state.run) renderCases(state.run);
  };

  $("#drawer-scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
}

async function boot() {
  bind();
  try {
    const health = await api("/health");
    try {
      const meta = await api("/meta");
      state.livePlanner = meta.live_planner || null;
    } catch (e) {
      // A missing /meta is not worth failing boot over; the button simply
      // stays hidden and every rationale comes from the fallback table.
      state.livePlanner = null;
    }
    if (health.mode !== "simulator") {
      const banner = $("#mode-banner");
      banner.innerHTML = "";
      banner.appendChild(el("b", null, "WARNING"));
      banner.appendChild(el("span", null, "Server is not reporting simulator mode."));
    }
    await refreshRunList();
    const select = $("#run-select");
    if (!select.disabled && select.value) {
      await loadRun(select.value);
      status("");
    } else {
      $("#empty-state").hidden = false;
      status("No runs yet. Start a run to extract, classify and plan a batch.");
    }
  } catch (err) {
    status("Could not reach the API: " + err.message, "err");
  }
}

boot();

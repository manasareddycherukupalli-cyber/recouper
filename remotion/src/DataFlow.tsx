/**
 * Recouper — how a batch actually flows through the system.
 *
 * Six scenes, following `BatchRunner.run()` in order. Every figure on screen
 * is the real output of `python -m recouper.cli run --no-llm` on seed 42, not
 * an illustration of what it might produce.
 *
 * The scene that matters is 04. Everything before it is plumbing; the claim
 * the project rests on is that the planner proposes and deterministic code
 * disposes, so that scene gets the most time and the clearest staging.
 */

import React from "react";
import {
  AbsoluteFill,
  Sequence,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { c, classes, data, rules } from "./theme";
import { Arrow, Figure, Node, Note, Reveal, Stage, archivo, mono, useCount, useEnter } from "./ui";

export const SCENES = [
  { name: "ingest", frames: 210 },
  { name: "classify", frames: 210 },
  { name: "split", frames: 240 },
  { name: "gate", frames: 420 },
  { name: "ledger", frames: 240 },
  { name: "measure", frames: 330 },
] as const;

export const TOTAL = SCENES.reduce((n, s) => n + s.frames, 0); // 1650 = 55s @30fps

// --- 01 · ingest ---------------------------------------------------------

const Ingest: React.FC = () => {
  const cases = useCount(data.cases, 40);
  const records = useCount(data.records, 12);
  return (
    <Stage
      n="STAGE 01"
      title="Find the money that was lost, not refused"
      sub="Three sources, one case type. An order carrying a failed payment satisfies two of them at once — counting it twice would inflate every rate that follows."
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, width: "100%" }}>
        <Node at={10} title={`${records} records`} file="providers/mock.py" width={300}>
          <div style={{ marginTop: 14, display: "grid", gap: 6 }}>
            {data.byKind.map((k, i) => (
              <Reveal
                key={k.label}
                at={18 + i * 6}
                style={{
                  fontFamily: mono,
                  fontSize: 17,
                  color: c.inkSoft,
                  display: "flex",
                  justifyContent: "space-between",
                  gap: 20,
                }}
              >
                <span>{k.label}</span>
                <span style={{ color: c.ink }}>{k.n}</span>
              </Reveal>
            ))}
          </div>
        </Node>

        <Arrow at={34} label="extract_cases()" width={130} />

        <Node at={40} title={`${cases} cases`} file="detect/score.py" tone="petrol" width={280} />

        <div style={{ marginLeft: 40, ...useEnter(64) }}>
          <div
            style={{
              fontFamily: mono,
              fontSize: 19,
              color: c.deny,
              border: `1px solid ${c.deny}`,
              background: c.denyBg,
              borderRadius: 4,
              padding: "14px 18px",
              maxWidth: 380,
              lineHeight: 1.5,
            }}
          >
            {data.deduped} orders skipped
            <div style={{ color: c.inkSoft, fontSize: 16, marginTop: 6 }}>
              already counted as failed payments
            </div>
          </div>
        </div>
      </div>
    </Stage>
  );
};

// --- 02 · classify -------------------------------------------------------

const Classify: React.FC = () => (
  <Stage
    n="STAGE 02"
    title="Decide what can honestly be done about each one"
    sub="Retrying a payment that cannot succeed burns gateway fees and trips issuer velocity limits that damage approval rates on good traffic. So every failure is sorted before anything is planned."
  >
    <div style={{ width: "100%", display: "grid", gap: 7 }}>
      {classes.map((k, i) => {
        const terminal = !k.contact;
        return (
          <Reveal
            key={k.name}
            at={14 + i * 9}
            style={{
              display: "grid",
              gridTemplateColumns: "280px 1fr 150px 150px",
              alignItems: "center",
              gap: 24,
              padding: "13px 20px",
              background: terminal ? c.denyBg : c.surface,
              border: `1px solid ${terminal ? c.deny : c.rule}`,
              borderRadius: 4,
            }}
          >
            <span style={{ fontFamily: archivo, fontSize: 23, fontWeight: 600, color: c.ink }}>
              {k.name}
            </span>
            <span style={{ fontFamily: mono, fontSize: 18, color: c.inkFaint }}>{k.eg}</span>
            <span
              style={{
                fontFamily: mono,
                fontSize: 17,
                color: k.retry ? c.allow : c.deny,
              }}
            >
              {k.retry ? "retry ok" : "no retry"}
            </span>
            <span
              style={{
                fontFamily: mono,
                fontSize: 17,
                color: k.contact ? c.allow : c.deny,
              }}
            >
              {k.contact ? "contact ok" : "no contact"}
            </span>
          </Reveal>
        );
      })}
    </div>
  </Stage>
);

// --- 03 · split ----------------------------------------------------------

const Split: React.FC = () => {
  const t = useCount(data.treated, 30);
  const ctl = useCount(data.control, 30);
  return (
    <Stage
      n="STAGE 03"
      title="Hold a fifth of the batch back, and never touch it"
      sub="No contact, no retry, not even a plan. Without an untouched arm there is nothing to compare against, and every recovery figure downstream is a number you have to take on faith."
    >
      <div style={{ width: "100%" }}>
        <div style={{ display: "flex", gap: 24, alignItems: "stretch" }}>
          <Node at={14} title={`${t} treated`} file="planned · gated · executed" tone="petrol" width={520}>
            <div style={{ display: "flex", gap: 4, marginTop: 18, flexWrap: "wrap" }}>
              {Array.from({ length: 60 }).map((_, i) => (
                <Reveal
                  key={i}
                  at={24 + i * 0.7}
                  distance={4}
                  style={{ width: 12, height: 12, background: c.petrol, borderRadius: 1 }}
                />
              ))}
            </div>
          </Node>

          <Node at={20} title={`${ctl} control`} file="observed only" width={420}>
            <div style={{ display: "flex", gap: 4, marginTop: 18, flexWrap: "wrap" }}>
              {Array.from({ length: 14 }).map((_, i) => (
                <Reveal
                  key={i}
                  at={30 + i * 2}
                  distance={4}
                  style={{
                    width: 12,
                    height: 12,
                    border: `1px solid ${c.inkFaint}`,
                    borderRadius: 1,
                  }}
                />
              ))}
            </div>
          </Node>
        </div>

        <div style={{ marginTop: 40 }}>
          <Note at={110}>
            Assignment is random rather than alternating: the case list is sorted by expected
            value, so any systematic rule would load one arm with the higher-value debts and bias
            the comparison from the outset.
          </Note>
        </div>
      </div>
    </Stage>
  );
};

// --- 04 · the gate -------------------------------------------------------

const Gate: React.FC = () => {
  const frame = useCurrentFrame();

  // The nine rules light up in order, then two of them return a verdict.
  const ruleLit = (i: number) => {
    const at = 96 + i * 7;
    return interpolate(frame, [at, at + 8], [0, 1], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });
  };
  const verdictIn = interpolate(frame, [186, 200], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <Stage
      n="STAGE 04"
      title="The planner proposes. Deterministic code disposes."
      sub="The model chooses how to approach a debt. It never decides whether an action is permitted."
    >
      <div style={{ width: "100%", display: "flex", gap: 28, alignItems: "flex-start" }}>
        {/* proposal */}
        <Node at={8} title="Planner" file="agent/plan.py · gemini · claude · table" width={430}>
          <div style={{ marginTop: 16, ...useEnter(30) }}>
            <div style={{ fontFamily: mono, fontSize: 17, color: c.inkFaint }}>
              inv_YbMkWX5o0EcK4C · ₹28,216
            </div>
            <div style={{ fontFamily: archivo, fontSize: 22, color: c.ink, marginTop: 10 }}>
              send_reminder → create_payment_link
            </div>
            <div
              style={{
                fontFamily: archivo,
                fontSize: 18,
                color: c.inkSoft,
                marginTop: 10,
                lineHeight: 1.5,
              }}
            >
              “Only 11 days overdue with a moderate amount. A polite reminder with a direct
              payment link gives friction-free resolution.”
            </div>
          </div>
        </Node>

        {/* the boundary, drawn as an actual line */}
        <div style={{ ...useEnter(60), alignSelf: "stretch", display: "flex", flexDirection: "column", alignItems: "center", gap: 12 }}>
          <div style={{ width: 2, flex: 1, background: c.petrol }} />
          <div
            style={{
              fontFamily: mono,
              fontSize: 14,
              color: c.petrol,
              letterSpacing: "0.14em",
              writingMode: "vertical-rl",
            }}
          >
            BOUNDARY
          </div>
          <div style={{ width: 2, flex: 1, background: c.petrol }} />
        </div>

        {/* the gate */}
        <div style={{ flex: 1 }}>
          <Node at={70} title="Policy engine" file="policy/engine.py · 9 rules, all evaluated">
            <div style={{ marginTop: 16, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6 }}>
              {rules.map((r, i) => {
                const lit = ruleLit(i);
                const blocking = r === "contact_cooldown";
                return (
                  <div
                    key={r}
                    style={{
                      fontFamily: mono,
                      fontSize: 16,
                      padding: "7px 11px",
                      borderRadius: 3,
                      opacity: 0.25 + lit * 0.75,
                      color: blocking && verdictIn > 0.5 ? c.deny : c.inkSoft,
                      background:
                        blocking && verdictIn > 0.5 ? c.denyBg : lit > 0.5 ? c.surfaceSunk : "transparent",
                      border: `1px solid ${
                        blocking && verdictIn > 0.5 ? c.deny : "transparent"
                      }`,
                    }}
                  >
                    {r}
                  </div>
                );
              })}
            </div>
          </Node>

          <div style={{ marginTop: 18, display: "grid", gap: 8, opacity: verdictIn }}>
            {[
              { a: "send_reminder", v: "ALLOW", tone: c.allow, bg: c.allowBg, why: "all nine checks passed" },
              {
                a: "create_payment_link",
                v: "DENY",
                tone: c.deny,
                bg: c.denyBg,
                why: "contact_cooldown — 72h across all debts",
              },
            ].map((row) => (
              <div
                key={row.a}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 16,
                  padding: "13px 18px",
                  background: row.bg,
                  border: `1px solid ${row.tone}`,
                  borderRadius: 4,
                }}
              >
                <span
                  style={{
                    fontFamily: mono,
                    fontSize: 15,
                    fontWeight: 700,
                    letterSpacing: "0.1em",
                    color: row.tone,
                    minWidth: 72,
                  }}
                >
                  {row.v}
                </span>
                <span style={{ fontFamily: mono, fontSize: 18, color: c.ink, minWidth: 250 }}>
                  {row.a}
                </span>
                <span style={{ fontFamily: archivo, fontSize: 18, color: c.inkSoft }}>
                  {row.why}
                </span>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 22 }}>
            <Note at={230}>
              The model proposed two contacts. The gate permitted one — the first consumed the
              cooldown. Its reasoning bought it nothing, because the gate never reads the
              rationale.
            </Note>
          </div>
        </div>
      </div>
    </Stage>
  );
};

// --- 05 · ledger ---------------------------------------------------------

const Ledger: React.FC = () => {
  const n = useCount(data.ledgerRecords, 30);
  const entries = [
    { seq: 51, actor: "llm", event: "plan_created", tone: c.petrol },
    { seq: 52, actor: "provider", event: "action_executed", tone: c.allow },
    { seq: 53, actor: "policy", event: "action_denied", tone: c.deny },
    { seq: 54, actor: "policy", event: "action_escalated", tone: c.escalate },
  ];
  return (
    <Stage
      n="STAGE 05"
      title="Write down the refusals as loudly as the actions"
      sub="Every decision — taken, denied or escalated — is appended to a JSONL ledger, each record hash-chained to the one before it."
    >
      <div style={{ width: "100%", display: "flex", gap: 44, alignItems: "center" }}>
        <div style={{ flex: 1, display: "grid", gap: 8 }}>
          {entries.map((e, i) => {
            return (
              <Reveal
                key={e.seq}
                at={16 + i * 14}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 18,
                  padding: "14px 20px",
                  background: c.surface,
                  border: `1px solid ${c.rule}`,
                  borderLeft: `3px solid ${e.tone}`,
                  borderRadius: 4,
                  fontFamily: mono,
                  fontSize: 19,
                }}
              >
                <span style={{ color: c.inkFaint, minWidth: 46 }}>{e.seq}</span>
                <span style={{ color: e.tone, minWidth: 110 }}>{e.actor}</span>
                <span style={{ color: c.ink, flex: 1 }}>{e.event}</span>
                <span style={{ color: c.inkFaint, fontSize: 16 }}>
                  prev ← {(0x9c04 + i).toString(16)}…
                </span>
              </Reveal>
            );
          })}
        </div>

        <div style={{ minWidth: 380 }}>
          <Figure at={30} value={String(n)} label="records for 340 cases" tone={c.petrol} />
          <div style={{ marginTop: 30 }}>
            <Note at={70}>
              Roughly half the ledger is things the system decided <i>not</i> to do. Edit, delete
              or reorder any record and verify() reports the exact sequence number where the chain
              broke.
            </Note>
          </div>
        </div>
      </div>
    </Stage>
  );
};

// --- 06 · measure --------------------------------------------------------

const Measure: React.FC = () => {
  const frame = useCurrentFrame();
  const scale = 0.26;
  const barW = (rate: number) =>
    interpolate(frame, [26, 52], [0, (rate / scale) * 620], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    });

  return (
    <Stage
      n="STAGE 06"
      title="Report the difference, including when there isn’t one"
      sub="Outcomes are drawn for both arms together, once, at the end of the batch — so the two arms cannot occupy different positions in the random stream."
    >
      <div style={{ width: "100%" }}>
        {[
          { label: "We contacted", n: data.treated, rate: data.treatedRate, tone: c.petrol },
          { label: "We left alone", n: data.control, rate: data.controlRate, tone: c.inkFaint },
        ].map((row, i) => (
          <Reveal
            key={row.label}
            at={10 + i * 10}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 24,
              marginBottom: 18,
            }}
          >
            <span style={{ fontFamily: archivo, fontSize: 24, color: c.ink, minWidth: 230 }}>
              {row.label}
              <span style={{ display: "block", fontSize: 17, color: c.inkFaint, marginTop: 3 }}>
                {row.n} cases
              </span>
            </span>
            <div style={{ width: 620, height: 34, background: c.surfaceSunk, borderRadius: 2 }}>
              <div style={{ width: barW(row.rate), height: "100%", background: row.tone, borderRadius: 2 }} />
            </div>
            <span
              style={{
                fontFamily: mono,
                fontSize: 26,
                color: c.ink,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {(row.rate * 100).toFixed(1)}%
            </span>
          </Reveal>
        ))}

        <div style={{ display: "flex", gap: 76, marginTop: 44, alignItems: "flex-start" }}>
          <Figure
            at={80}
            value={`${(data.lift * 100).toFixed(1)} pts`}
            label="difference between the arms"
            tone={c.deny}
          />
          <div style={{ ...useEnter(96), paddingTop: 8 }}>
            <div
              style={{
                fontFamily: mono,
                fontSize: 19,
                color: c.escalate,
                border: `1px solid ${c.escalate}`,
                background: c.escalateBg,
                borderRadius: 3,
                padding: "8px 14px",
                display: "inline-block",
              }}
            >
              NOT SIGNIFICANT
            </div>
            <div style={{ fontFamily: mono, fontSize: 19, color: c.inkSoft, marginTop: 12 }}>
              95% CI [{(data.ciLow * 100).toFixed(1)}%, +{(data.ciHigh * 100).toFixed(1)}%]
            </div>
          </div>
        </div>

        <div style={{ marginTop: 36 }}>
          <Note at={150}>
            The interval crosses zero, so this batch does not show the intervention beat doing
            nothing — and it says so. A framework that always reports success is not measuring
            anything.
          </Note>
        </div>
      </div>
    </Stage>
  );
};

// --- composition ---------------------------------------------------------

const SCENE_COMPONENTS = [Ingest, Classify, Split, Gate, Ledger, Measure];

export const DataFlow: React.FC = () => {
  const { durationInFrames } = useVideoConfig();
  const frame = useCurrentFrame();
  let from = 0;

  return (
    <AbsoluteFill style={{ background: c.ground }}>
      {SCENES.map((s, i) => {
        const Comp = SCENE_COMPONENTS[i];
        const start = from;
        from += s.frames;
        return (
          <Sequence key={s.name} from={start} durationInFrames={s.frames}>
            <Comp />
          </Sequence>
        );
      })}

      {/* progress hairline, so a viewer can see how far in they are */}
      <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 3, background: c.ruleSoft }}>
        <div
          style={{
            height: "100%",
            width: `${(frame / durationInFrames) * 100}%`,
            background: c.petrol,
          }}
        />
      </div>
    </AbsoluteFill>
  );
};

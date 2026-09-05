/**
 * Shared primitives for the data-flow video.
 *
 * The type split mirrors the rest of the project: Archivo for labels a human
 * wrote, JetBrains Mono for anything the machine produced -- ids, counts,
 * verdicts, rule names. Keeping that consistent is what makes the video read
 * as the same system as the console rather than a slide deck about it.
 */

import React from "react";
import { interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";
import { loadFont as loadArchivo } from "@remotion/google-fonts/Archivo";
import { loadFont as loadMono } from "@remotion/google-fonts/JetBrainsMono";
import { c } from "./theme";

export const { fontFamily: archivo } = loadArchivo();
export const { fontFamily: mono } = loadMono();

/** Fade + rise, delayed by `at` frames. The one entrance used everywhere. */
export const useEnter = (at: number, distance = 14) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const s = spring({
    frame: frame - at,
    fps,
    config: { damping: 200, mass: 0.6 },
  });
  return {
    opacity: s,
    transform: `translateY(${interpolate(s, [0, 1], [distance, 0])}px)`,
  };
};

/** Count up to `to`, reaching it `dur` frames after `at`. */
export const useCount = (to: number, at: number, dur = 26) => {
  const frame = useCurrentFrame();
  return Math.round(
    interpolate(frame, [at, at + dur], [0, to], {
      extrapolateLeft: "clamp",
      extrapolateRight: "clamp",
    })
  );
};

/**
 * Wraps children in the standard entrance.
 *
 * Exists so that a list can animate its items without calling a hook inside a
 * `.map()` callback -- that happens to work while the array length is fixed,
 * but it violates the rules of hooks and breaks the moment a list becomes
 * data-driven. The hook belongs in a component.
 */
export const Reveal: React.FC<{
  at: number;
  distance?: number;
  style?: React.CSSProperties;
  /** Optional: the dot grids in scene 03 reveal an empty box. */
  children?: React.ReactNode;
}> = ({ at, distance, style, children }) => {
  const e = useEnter(at, distance);
  return <div style={{ ...e, ...style }}>{children}</div>;
};

export const Stage: React.FC<{
  n: string;
  title: string;
  sub?: string;
  children?: React.ReactNode;
}> = ({ n, title, sub, children }) => {
  const head = useEnter(0);
  return (
    <div style={{ padding: "72px 96px", height: "100%", display: "flex", flexDirection: "column" }}>
      <div style={{ ...head }}>
        <div
          style={{
            fontFamily: mono,
            fontSize: 20,
            letterSpacing: "0.18em",
            color: c.petrol,
            marginBottom: 14,
          }}
        >
          {n}
        </div>
        <div
          style={{
            fontFamily: archivo,
            fontSize: 58,
            fontWeight: 700,
            letterSpacing: "-0.025em",
            color: c.ink,
            lineHeight: 1.05,
          }}
        >
          {title}
        </div>
        {sub ? (
          <div
            style={{
              fontFamily: archivo,
              fontSize: 24,
              color: c.inkSoft,
              marginTop: 14,
              maxWidth: 1180,
              lineHeight: 1.45,
            }}
          >
            {sub}
          </div>
        ) : null}
      </div>
      {/* Top-aligned: centring left a large gap under the subtitle on every
          scene, and the tall scenes need the vertical room anyway. */}
      <div style={{ flex: 1, display: "flex", alignItems: "flex-start", marginTop: 44 }}>
        {children}
      </div>
    </div>
  );
};

/** A labelled box representing one module in the pipeline. */
export const Node: React.FC<{
  at: number;
  title: string;
  file?: string;
  tone?: "plain" | "petrol" | "allow" | "deny" | "escalate";
  width?: number;
  children?: React.ReactNode;
}> = ({ at, title, file, tone = "plain", width, children }) => {
  const e = useEnter(at);
  const edge =
    tone === "petrol" ? c.petrol
    : tone === "allow" ? c.allow
    : tone === "deny" ? c.deny
    : tone === "escalate" ? c.escalate
    : c.rule;
  const fill =
    tone === "petrol" ? c.petrolBg
    : tone === "allow" ? c.allowBg
    : tone === "deny" ? c.denyBg
    : tone === "escalate" ? c.escalateBg
    : c.surface;

  return (
    <div
      style={{
        ...e,
        width,
        background: fill,
        border: `1px solid ${edge}`,
        borderRadius: 4,
        padding: "20px 24px",
      }}
    >
      <div style={{ fontFamily: archivo, fontSize: 26, fontWeight: 600, color: c.ink }}>
        {title}
      </div>
      {file ? (
        <div style={{ fontFamily: mono, fontSize: 17, color: c.inkFaint, marginTop: 6 }}>
          {file}
        </div>
      ) : null}
      {children}
    </div>
  );
};

/** A horizontal connector that draws itself left-to-right. */
export const Arrow: React.FC<{ at: number; width?: number; label?: string }> = ({
  at,
  width = 88,
  label,
}) => {
  const frame = useCurrentFrame();
  const grow = interpolate(frame, [at, at + 12], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <div style={{ width, display: "flex", flexDirection: "column", alignItems: "center", gap: 8 }}>
      {label ? (
        <div style={{ fontFamily: mono, fontSize: 14, color: c.inkFaint, opacity: grow }}>
          {label}
        </div>
      ) : null}
      <div style={{ width: "100%", height: 2, background: c.rule, position: "relative" }}>
        <div
          style={{
            position: "absolute",
            inset: 0,
            width: `${grow * 100}%`,
            background: c.petrol,
          }}
        />
        <div
          style={{
            position: "absolute",
            right: -1,
            top: -4,
            width: 0,
            height: 0,
            borderTop: "5px solid transparent",
            borderBottom: "5px solid transparent",
            borderLeft: `9px solid ${c.petrol}`,
            opacity: grow > 0.95 ? 1 : 0,
          }}
        />
      </div>
    </div>
  );
};

export const Figure: React.FC<{
  at: number;
  value: string;
  label: string;
  tone?: string;
}> = ({ at, value, label, tone = c.ink }) => {
  const e = useEnter(at);
  return (
    <div style={{ ...e }}>
      <div
        style={{
          fontFamily: mono,
          fontSize: 62,
          fontWeight: 700,
          color: tone,
          lineHeight: 1,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {value}
      </div>
      <div style={{ fontFamily: archivo, fontSize: 20, color: c.inkSoft, marginTop: 10 }}>
        {label}
      </div>
    </div>
  );
};

/** The line that closes each scene. Serif, because it is a judgment. */
export const Note: React.FC<{ at: number; children: React.ReactNode }> = ({ at, children }) => {
  const e = useEnter(at);
  return (
    <div
      style={{
        ...e,
        fontFamily: archivo,
        fontSize: 22,
        color: c.inkSoft,
        borderLeft: `2px solid ${c.petrol}`,
        paddingLeft: 18,
        maxWidth: 1080,
        lineHeight: 1.5,
      }}
    >
      {children}
    </div>
  );
};

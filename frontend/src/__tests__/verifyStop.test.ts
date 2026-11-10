/** The Verify stop's where / ready when / who / notes / post to PR
 * (lib/verifyStop.ts, components/flights/VerifyStopFields.tsx) and a Verify
 * repo's environments (components/intake/RepoEnvironments.tsx): the face
 * sentence, a legacy `wait_live`, the where picker's options, when Minutes
 * shows, the environment name rules, and the editor as it renders. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { TestPlan } from "../api/types";
import type { FlightStep, StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));

const V = await import("../lib/verifyStop");
const { stopSummary } = await import("../lib/flightDiagram");
const { StopForm } = await import("../components/flights/StopParts");
const { VerifyStopBehaviors, verifySteps, isVerifyEditor } = await import("../components/flights/VerifyStopFields");
const { RepoEnvironments } = await import("../components/intake/RepoEnvironments");
const { fieldPatch } = await import("../components/intake/repoUses");
const { queryClient } = await import("../state/queries");

const ENVS = {
  repos: {
    "acme/shop": [
      { name: "live", reach: "https://shop.example.com", branch: "release", deploy_minutes: 5, implicit: true },
      { name: "sandbox", reach: "https://sandbox.shop.example.com", branch: "sandbox", deploy_minutes: 10 },
    ],
    "acme/api": [
      { name: "live", reach: "", branch: "release", deploy_minutes: 3, implicit: true },
      { name: "staging", reach: "#api-staging on Slack", branch: "staging", deploy_minutes: 0 },
      { name: "sandbox", reach: "", branch: "sbx", deploy_minutes: 2 },
    ],
  },
  names: ["live", "sandbox", "staging"],
};

const face = (c: Record<string, unknown>) => V.verifyFace(c, ENVS);

describe("the stop's face: Verify on <where> · <who> · <ready>", () => {
  it("the contract's examples", () => {
    expect(face({ where: "sandbox", who: "both", ready: "after_prev", ready_minutes: 15 })).toBe("Verify on sandbox · agent, then you · 15 min after the stop before");
    expect(face({ where: "branch", who: "agent", ready: "now" })).toBe("Verify on this branch · agent only · right away");
    expect(face({ where: "live", who: "both", ready: "on_branch" })).toBe("Verify on live · agent, then you · once on release");
  });

  it("every who", () => {
    expect(face({ who: "both" })).toBe("Verify on this branch · agent, then you · right away");
    expect(face({ who: "agent" })).toBe("Verify on this branch · agent only · right away");
    expect(face({ who: "you" })).toBe("Verify on this branch · you · right away");
    expect(face({ who: "nonsense" })).toBe("Verify on this branch · agent, then you · right away");
  });

  it("every ready, on an environment", () => {
    expect(face({ where: "staging", ready: "now" })).toBe("Verify on staging · agent, then you · right away");
    expect(face({ where: "staging", ready: "on_branch" })).toBe("Verify on staging · agent, then you · once on staging");
    expect(face({ where: "staging", ready: "on_branch", ready_minutes: 20 })).toBe("Verify on staging · agent, then you · once on staging + 20 min");
    expect(face({ where: "staging", ready: "after_prev", ready_minutes: 30 })).toBe("Verify on staging · agent, then you · 30 min after the stop before");
    // 0 minutes = the environment's own deploy time.
    expect(face({ where: "staging", ready: "after_prev", ready_minutes: 0 })).toBe("Verify on staging · agent, then you · after the deploy time");
  });

  it("every ready, on this branch: no environment branch to reach, no deploy to wait for", () => {
    expect(face({ where: "branch", ready: "on_branch" })).toBe("Verify on this branch · agent, then you · right away");
    expect(face({ where: "branch", ready: "after_prev", ready_minutes: 5 })).toBe("Verify on this branch · agent, then you · 5 min after the stop before");
    expect(face({ where: "branch", ready: "after_prev" })).toBe("Verify on this branch · agent, then you · after the stop before");
  });

  it("once on <the env's branch> only when every repo agrees on it, else its name", () => {
    expect(V.envBranch("live", ENVS)).toBe("release");
    expect(V.envBranch("sandbox", ENVS)).toBe("");
    expect(face({ where: "sandbox", ready: "on_branch" })).toBe("Verify on sandbox · agent, then you · once on sandbox");
    expect(V.verifyFace({ where: "live", ready: "on_branch" }, null)).toBe("Verify on live · agent, then you · once on live");
  });

  it("the diagram's subtitle is the face, reading the environments the query remembered", () => {
    V.rememberEnvironments(ENVS);
    try {
      expect(stopSummary({ kind: "verify", config: { where: "live", ready: "on_branch", who: "you" } })).toBe("Verify on live · you · once on release");
    } finally {
      V.rememberEnvironments(null);
    }
    expect(stopSummary({ kind: "verify", config: { where: "live", ready: "on_branch", who: "you" } })).toBe("Verify on live · you · once on live");
  });
});

describe("a legacy config", () => {
  it("wait_live true reads as where live, ready on_branch", () => {
    const v = V.verifyConfig({ who: "agent", wait_live: true });
    expect(v).toMatchObject({ where: "live", ready: "on_branch", who: "agent", ready_minutes: 0, notes: "", post_to_pr: false });
    expect(face({ who: "agent", wait_live: true })).toBe("Verify on live · agent only · once on release");
  });

  it("wait_live false, or new fields already set, change nothing", () => {
    expect(V.verifyConfig({ wait_live: false })).toMatchObject({ where: "branch", ready: "now" });
    expect(V.verifyConfig({ wait_live: true, where: "sandbox", ready: "after_prev" })).toMatchObject({ where: "sandbox", ready: "after_prev" });
  });

  it("a change saves the new fields and drops wait_live", () => {
    const out = V.withVerifyChoice({ who: "agent", wait_live: true }, { who: "you" });
    expect(out).toEqual({ where: "live", ready: "on_branch", ready_minutes: 0, who: "you", notes: "", post_to_pr: false });
  });

  it("defaults fill in; minutes are clamped", () => {
    expect(V.verifyConfig(undefined)).toEqual({ where: "branch", ready: "now", ready_minutes: 0, who: "both", notes: "", post_to_pr: false });
    expect(V.verifyConfig({ ready_minutes: 99999 }).ready_minutes).toBe(1440);
    expect(V.verifyConfig({ ready_minutes: -3 }).ready_minutes).toBe(0);
  });
});

describe("ready when and its minutes", () => {
  it("on this branch, 'When it reaches the environment's branch' is not offered", () => {
    expect(V.readyChoices("branch").map((o) => o.value)).toEqual(["now", "after_prev"]);
    expect(V.readyChoices("sandbox").map((o) => o.label)).toEqual(["Right away", "When it reaches the environment's branch", "Minutes after the stop before"]);
  });

  it("picking this branch while on_branch moves ready to now", () => {
    expect(V.withVerifyChoice({ where: "sandbox", ready: "on_branch" }, { where: "branch" })).toMatchObject({ where: "branch", ready: "now" });
    expect(V.withVerifyChoice({ where: "sandbox", ready: "after_prev" }, { where: "branch" })).toMatchObject({ where: "branch", ready: "after_prev" });
  });

  it("Minutes shows only for on_branch and after_prev", () => {
    expect(V.showsReadyMinutes("now")).toBe(false);
    expect(V.showsReadyMinutes("on_branch")).toBe(true);
    expect(V.showsReadyMinutes("after_prev")).toBe(true);
    expect(V.READY_MINUTES_HINT).toBe("0 = the environment's deploy time");
  });
});

describe("the where picker's options", () => {
  it("this branch's code first, then every environment once, each with its repos as the hover", () => {
    const opts = V.whereOptions(ENVS, "branch");
    expect(opts.map((o) => o.value)).toEqual(["branch", "live", "sandbox", "staging"]);
    expect(opts[0].label).toBe("This branch's code");
    const sandbox = opts.find((o) => o.value === "sandbox")!;
    expect(sandbox.title).toContain("acme/shop: https://sandbox.shop.example.com · deploys from sandbox · 10 min to deploy");
    expect(sandbox.title).toContain("acme/api: deploys from sbx · 2 min to deploy");
  });

  it("a value no repo has stays visible as its own option; 'branch' is never an environment", () => {
    const opts = V.whereOptions({ repos: { "a/b": [{ name: "branch", reach: "", branch: "", deploy_minutes: 0 }] }, names: ["branch", "qa"] }, "preview");
    expect(opts.map((o) => o.value)).toEqual(["branch", "qa", "preview"]);
    expect(opts[2].custom).toBe(true);
  });

  it("no list (loading, an older server): just this branch and the current value", () => {
    expect(V.whereOptions(null, "live").map((o) => o.value)).toEqual(["branch", "live"]);
  });
});

describe("an environment's name: the server's rules", () => {
  it("1..40 lowercase letters, digits, - and _", () => {
    expect(V.envNameError("sandbox")).toBe("");
    expect(V.envNameError("qa-2_eu")).toBe("");
    expect(V.envNameError("")).toBe("Give it a name");
    expect(V.envNameError("   ")).toBe("Give it a name");
    expect(V.envNameError("Sandbox")).toContain("Lowercase");
    expect(V.envNameError("my env")).toContain("Lowercase");
    expect(V.envNameError("a".repeat(40))).toBe("");
    expect(V.envNameError("a".repeat(41))).toContain("40");
  });

  it("not 'branch', unique per repo", () => {
    expect(V.envNameError("branch")).toContain("branch");
    expect(V.envNameError("live", ["live"])).toContain("Another");
    expect(V.envRowErrors([{ name: "qa" }, { name: "qa" }, { name: "live" }])).toEqual(["Another environment has this name", "Another environment has this name", ""]);
  });

  it("the list saves trimmed and clamped, leaves untouched new rows out, and waits for valid names", () => {
    const row = (over: Partial<import("../lib/verifyStop").VerifyEnv>) => ({ name: "", reach: "", branch: "", deploy_minutes: 0, ...over });
    expect(V.envsToSave([row({ name: " qa ", reach: " https://qa ", branch: " qa ", deploy_minutes: 5000 }), row({})])).toEqual([{ name: "qa", reach: "https://qa", branch: "qa", deploy_minutes: 1440 }]);
    expect(V.envsToSave([row({ name: "qa" }), row({ reach: "somewhere" })])).toBeNull();
    expect(V.envsToSave([row({ name: "QA" })])).toBeNull();
    expect(V.envsOf({ environments: [{ name: "qa", deploy_minutes: "7" }, null, 3] })).toEqual([{ name: "qa", reach: "", branch: "", deploy_minutes: 7 }]);
    expect(V.envsOf({})).toEqual([]);
  });

  it("the card stores the list whole through the same field patch; empty removes it", () => {
    const list = [{ name: "qa", reach: "", branch: "qa", deploy_minutes: 0 }];
    const a = fieldPatch({ "acme/shop": { live_branch: "release" } }, "verify", "acme/shop", "environments", list);
    expect(a.patch).toEqual({ repository: { verify_repo_settings: { "acme/shop": { live_branch: "release", environments: list } } } });
    const b = fieldPatch(a.blocks, "verify", "acme/shop", "environments", []);
    expect(b.blocks).toEqual({ "acme/shop": { live_branch: "release" } });
  });
});

// --- The editor as it renders ------------------------------------------------------------

const VERIFY: StepKindDef = {
  kind: "verify",
  label: "Verify",
  actor: "robot",
  summary: "Check the behaviors",
  default_title: "Verify",
  fields: [
    { name: "where", type: "text", label: "Where", default: "branch", placeholder: "branch or an environment name" },
    { name: "ready", type: "select", label: "Ready when", options: [{ value: "now", label: "Right away" }, { value: "on_branch", label: "When it reaches the environment's branch" }, { value: "after_prev", label: "Minutes after the stop before" }] },
    { name: "ready_minutes", type: "int", label: "Minutes" },
    { name: "who", type: "select", label: "Who checks", options: [{ value: "both", label: "Agent, then me" }, { value: "agent", label: "Agent only" }, { value: "you", label: "Only me" }] },
    { name: "notes", type: "textarea", label: "Notes for this check" },
    { name: "post_to_pr", type: "bool", label: "Post the results to the PR" },
  ],
};
const verifyStep = (config: Record<string, unknown>): FlightStep => ({ id: "v1", kind: "verify", title: "Verify", config, on_fail: "halt" });
const form = (config: Record<string, unknown>, def: StepKindDef = VERIFY) => renderToStaticMarkup(createElement(StopForm, { step: verifyStep(config), def, onChange: () => {} }));

describe("the Verify stop's editor", () => {
  it("is drawn for the new kind only; an older server's Verify keeps the generic form", () => {
    expect(isVerifyEditor("verify", VERIFY)).toBe(true);
    expect(isVerifyEditor("verify", { fields: [{ name: "who", type: "select", label: "Who" }, { name: "wait_live", type: "bool", label: "Wait" }] })).toBe(false);
    expect(isVerifyEditor("ship", VERIFY)).toBe(false);
  });

  it("an explainer of three numbered lines, naming where", () => {
    const html = form({ where: "sandbox" });
    expect(html).toContain('class="vf-explain fl-fld-wide"');
    expect(html).toContain("behaviors are written from the branch&#x27;s changes");
    expect(html).toContain("the agent checks its ones on sandbox");
    expect(html).toContain("yours park the route here (a perch)");
    expect(verifySteps({ where: "branch", who: "agent" })).toEqual(["behaviors are written from the branch's changes", "the agent walks every one on this branch's code, yours too", "it asks you only for what it can't do — the route goes on"]);
    expect(verifySteps({ where: "live", who: "you" })[1]).toBe("you check every one on live");
  });

  it("who checks: three choices, each with its one line, Agent, then me by default", () => {
    const html = form({});
    expect(html).toMatch(/role="radio" aria-checked="true" class="vf-who-opt on" data-value="both"><strong>Agent, then me<\/strong>/);
    expect(html).toContain("The agent checks what it can; the ones only a person can check park the route here for you.");
    expect(html).toContain("<strong>Agent only</strong><span class=\"muted\">The agent walks every behavior, yours too — it asks you only for what it can&#x27;t do.</span>");
    expect(html).toContain("<strong>Only me</strong><span class=\"muted\">Every check waits for you.</span>");
  });

  it("where: this branch's code, the environments, Other…; ready without on_branch on this branch; Minutes hidden for now", () => {
    queryClient.setQueryData(["test-plan-environments"], ENVS);
    try {
      const html = form({ where: "branch" });
      for (const label of ["This branch&#x27;s code", ">live<", ">sandbox<", ">staging<", "Other…"]) expect(html).toContain(label);
      expect(html).not.toContain("When it reaches the environment&#x27;s branch");
      expect(html).not.toContain('aria-label="Minutes (min)"');
      const env = form({ where: "staging", ready: "on_branch" });
      expect(env).toContain("When it reaches the environment&#x27;s branch");
      expect(env).toContain('aria-label="Minutes (min)"');
      expect(env).toContain("0 = the environment&#x27;s deploy time");
      expect(env).toContain("acme/api: #api-staging on Slack · deploys from staging");
    } finally {
      queryClient.removeQueries({ queryKey: ["test-plan-environments"] });
    }
  });

  it("notes and post to PR; never the generic `where` text field or the old `who` chips", () => {
    const html = form({ notes: "use the QA login", post_to_pr: true });
    expect(html).toContain("use the QA login");
    expect(html).toMatch(/class="fl-toggle on" aria-pressed="true"[^>]*>.*Post the results to the PR/);
    expect(html).not.toContain('placeholder="branch or an environment name"');
    expect(html).not.toContain(">The agent<");
  });

  it("a legacy wait_live config opens on live, when it reaches the branch", () => {
    const html = form({ who: "agent", wait_live: true });
    expect(html).toContain("the agent walks every one on live, yours too");
    expect(html).toMatch(/class="on" aria-pressed="true">When it reaches the environment&#x27;s branch/);
  });
});

describe("the stop's behaviors inside it", () => {
  const plan = {
    id: "p9",
    steps: [
      { id: "a", text: "the export downloads", expect: "", actor: "agent" },
      { id: "b", text: "the chart looks right", expect: "", actor: "human" },
    ],
    runs: [],
  } as unknown as TestPlan;

  it("each one marked agent or you, with the count of yours", () => {
    queryClient.setQueryData(["test-plans"], { plans: [plan], live_branch: "main" });
    try {
      const html = renderToStaticMarkup(createElement(VerifyStopBehaviors, { planId: "p9" }));
      expect(html).toContain("Behaviors — 2 · 1 yours");
      expect(html).toMatch(/the export downloads<\/span><span class="vf-behavior-who"[^>]*>🤖 agent</);
      expect(html).toMatch(/class="vf-behavior vf-yours"[^>]*>.*the chart looks right<\/span><span class="vf-behavior-who"[^>]*>🙋 you</);
      expect(renderToStaticMarkup(createElement(VerifyStopBehaviors, { planId: "nope" }))).toBe("");
    } finally {
      queryClient.removeQueries({ queryKey: ["test-plans"] });
    }
  });
});

describe("a Verify repo's environments", () => {
  it("rows of name, how to reach it, deploys from branch, deploy time; + Add", () => {
    const html = renderToStaticMarkup(createElement(RepoEnvironments, { value: [{ name: "sandbox", reach: "https://sbx", branch: "sandbox", deploy_minutes: 10 }], onSave: () => {} }));
    expect(html).toContain(">Environments<");
    expect(html).toContain('data-env="sandbox"');
    for (const l of [">Name<", ">How to reach it<", ">Deploys from branch<", ">Deploy time (minutes)<", "+ Add an environment"]) expect(html).toContain(l);
    expect(html).toContain("placeholder=\"URL, Slack channel, log search… or &#x27;use what you know about this repo&#x27;\"");
    expect(html).toContain('value="10"');
  });
});

describe("the server's option_labels", () => {
  it("name a select's values when present; the UI's own words otherwise", async () => {
    const { fieldOptions, optionLabel } = await import("../lib/flights");
    const f = { name: "who", type: "select" as const, label: "Who", options: ["both", { value: "agent", label: "Agent" }, "you"], option_labels: { both: "Agent, then me", you: " " } };
    expect(fieldOptions(f)).toEqual([
      { value: "both", label: "Agent, then me" },
      { value: "agent", label: "Agent" },
      { value: "you", label: "you" },
    ]);
    expect(optionLabel(undefined, "x", "fallback")).toBe("fallback");
  });

  it("the Verify editor's Ready and Who choices wear them", () => {
    const def: StepKindDef = {
      ...VERIFY,
      fields: VERIFY.fields.map((f) =>
        f.name === "ready" ? { ...f, option_labels: { now: "Straight away" } } : f.name === "who" ? { ...f, option_labels: { you: "Just me" } } : f
      ),
    };
    const html = form({}, def);
    expect(html).toContain(">Straight away<");
    expect(html).toContain("<strong>Just me</strong>");
    expect(html).toContain("<strong>Agent, then me</strong>");
  });
});

describe("a 'noted' history event", () => {
  it("reads as its note alone in the stop's log", async () => {
    const { stopLog } = await import("../components/flights/useSessionLine");
    const step = verifyStep({ post_to_pr: true });
    const node = { id: "stop:v1", kind: "stop", row: "main", title: "Verify", sub: "", state: "done", icon: "verify", step, index: 0 } as never;
    const flight = {
      id: "f1",
      steps: [step],
      step_states: {},
      history: [
        { at: 100, step: "v1", event: "done", note: "2 behaviors passed" },
        { at: 101, step: "v1", event: "noted", note: "posted the results to PR #12" },
      ],
    } as never;
    const html = renderToStaticMarkup(createElement("div", null, stopLog(node, flight, undefined, undefined)));
    expect(html).toMatch(/<li data-event="noted"><span class="muted">[^<]*<\/span> posted the results to PR #12<\/li>/);
    expect(html).not.toContain("<b>noted</b>");
    expect(html).toContain("<b>done</b> — 2 behaviors passed");
  });
});

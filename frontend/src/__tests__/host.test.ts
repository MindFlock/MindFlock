/** The extension host's pure seams: target strings (pane keys / dialogTarget)
 * and the command-routing decision behind runCommand. Kept to the exported pure
 * functions — activation, mounting and the store are exercised only in the
 * browser. host.ts imports the UI store, toast and the api wrapper, all of
 * which are inert at import time under the node environment (setup.ts). */

import { describe, it, expect } from "vitest";
import { buildTarget, parseTarget, routeCommand } from "../extensions/host";
import type { ExtensionSpec } from "../extensions/types";

describe("buildTarget / parseTarget", () => {
  it("round-trips ext + surface with no ref", () => {
    expect(buildTarget("dbclient", "main")).toBe("dbclient:main");
    expect(parseTarget("dbclient:main")).toEqual({ extId: "dbclient", surfaceId: "main" });
    // No `ref` key at all — callers test `if (ref)`.
    expect("ref" in parseTarget("dbclient:main")).toBe(false);
  });
  it("treats an empty ref as no ref", () => {
    expect(buildTarget("dbclient", "main", "")).toBe("dbclient:main");
    expect(parseTarget(buildTarget("dbclient", "main", ""))).toEqual({
      extId: "dbclient",
      surfaceId: "main",
    });
  });
  it("round-trips a plain ref", () => {
    const t = buildTarget("dbclient", "query", "#3");
    expect(t).toBe("dbclient:query:#3");
    expect(parseTarget(t)).toEqual({ extId: "dbclient", surfaceId: "query", ref: "#3" });
  });
  it("keeps a ref that itself contains colons intact (indexOf slices, not split)", () => {
    // A table ref that carries its own hierarchy — exactly what an extension
    // would mint for "connection:database:schema:table".
    const ref = "conn-1:prod:public:orders";
    const t = buildTarget("dbclient", "table", ref);
    expect(parseTarget(t)).toEqual({ extId: "dbclient", surfaceId: "table", ref });
    // Including a ref that starts or ends with a colon.
    expect(parseTarget(buildTarget("a", "b", ":x:")).ref).toBe(":x:");
  });
  it("degrades a bare extension id to an empty surface", () => {
    expect(parseTarget("dbclient")).toEqual({ extId: "dbclient", surfaceId: "" });
  });
});

const SPEC: ExtensionSpec = {
  module: "/extensions/dbclient/index.js",
  bar_label: "Database",
  buttons: [],
  commands: [
    { id: "dbclient.explorer", title: "Database: Explorer", surface: "main" },
    { id: "dbclient.add-connection", title: "Database: Add connection", surface: "main", ref: "new" },
    { id: "dbclient.new-query", title: "Database: New query", surface: "query", ref: "fresh" },
    { id: "dbclient.sql", title: "Database: SQL" },
    // A manifest bug the backend validator would normally reject: a surface
    // that does not exist. The router must not open anything for it.
    { id: "dbclient.broken", title: "Broken", surface: "nope" },
  ],
  surfaces: [
    { id: "main", kind: "dialog", title: "Database Client" },
    { id: "query", kind: "pane", title: "SQL", multi: true, back_command: "dbclient.explorer" },
  ],
  stylesheet: true,
  api_version: 1,
};

describe("routeCommand", () => {
  it("a registered handler always wins, even over a declarative surface", () => {
    for (const status of ["idle", "loading", "active", "error"] as const) {
      expect(routeCommand("dbclient.explorer", { registered: true, status, spec: SPEC })).toEqual({
        kind: "handler",
      });
    }
    expect(routeCommand("dbclient.sql", { registered: true, status: "active", spec: SPEC })).toEqual({
      kind: "handler",
    });
  });
  it("opens a declarative dialog surface without the module (ref optional)", () => {
    expect(routeCommand("dbclient.explorer", { registered: false, status: "idle", spec: SPEC })).toEqual({
      kind: "dialog",
      surfaceId: "main",
      ref: undefined,
    });
    expect(
      routeCommand("dbclient.add-connection", { registered: false, status: "idle", spec: SPEC })
    ).toEqual({ kind: "dialog", surfaceId: "main", ref: "new" });
  });
  it("opens a declarative pane surface with its ref", () => {
    expect(routeCommand("dbclient.new-query", { registered: false, status: "active", spec: SPEC })).toEqual({
      kind: "pane",
      surfaceId: "query",
      ref: "fresh",
    });
  });
  it("routes declaratively whatever the activation status (the manifest needs no code)", () => {
    for (const status of ["idle", "loading", "active", "error"] as const) {
      expect(routeCommand("dbclient.explorer", { registered: false, status, spec: SPEC }).kind).toBe(
        "dialog"
      );
    }
  });
  it("activates first for an unregistered custom command while idle or loading", () => {
    expect(routeCommand("dbclient.sql", { registered: false, status: "idle", spec: SPEC })).toEqual({
      kind: "activate",
    });
    expect(routeCommand("dbclient.sql", { registered: false, status: "loading", spec: SPEC })).toEqual({
      kind: "activate",
    });
  });
  it("reports unknown for an unregistered custom command once active or failed", () => {
    expect(routeCommand("dbclient.sql", { registered: false, status: "active", spec: SPEC })).toEqual({
      kind: "unknown",
    });
    expect(routeCommand("dbclient.sql", { registered: false, status: "error", spec: SPEC })).toEqual({
      kind: "unknown",
    });
  });
  it("treats a command whose surface is missing as non-declarative", () => {
    // Falls through to activate/unknown rather than opening nothing.
    expect(routeCommand("dbclient.broken", { registered: false, status: "idle", spec: SPEC })).toEqual({
      kind: "activate",
    });
    expect(routeCommand("dbclient.broken", { registered: false, status: "active", spec: SPEC })).toEqual({
      kind: "unknown",
    });
  });
  it("treats a command absent from the manifest like any unregistered custom one", () => {
    expect(routeCommand("dbclient.nothing", { registered: false, status: "idle", spec: SPEC })).toEqual({
      kind: "activate",
    });
    expect(routeCommand("dbclient.nothing", { registered: false, status: "active", spec: SPEC })).toEqual({
      kind: "unknown",
    });
  });
});
